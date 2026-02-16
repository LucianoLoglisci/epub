import os
import re
import uuid
import threading
import queue
import asyncio
import inspect
import zipfile

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

import ebooklib
from ebooklib import epub
from lxml import html as lxml_html
import posixpath
import html as pyhtml  # FIX: html.escape corretto (non lxml.html)

import httpx
from googletrans import Translator


# =========================================================
# TRADUZIONE (robusta + cache)
# =========================================================
try:
    translator = Translator(timeout=30)  # alcune versioni lo accettano
except TypeError:
    translator = Translator()

TRANSLATION_CACHE = {}  # (text, lang) -> translated


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


async def translate_text(text, target_lang="it", retries=4, base_delay=0.8):
    """Traduce una stringa (con cache + retry)."""
    text = (text or "").strip()
    if not text:
        return text

    key = (text, target_lang)
    if key in TRANSLATION_CACHE:
        return TRANSLATION_CACHE[key]

    last_exc = None
    for attempt in range(retries):
        try:
            res = translator.translate(text, dest=target_lang)
            res = await _maybe_await(res)
            out = res.text
            TRANSLATION_CACHE[key] = out
            return out
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.HTTPError, Exception) as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))

    raise last_exc


# =========================================================
# EPUB: export risorse (ESTRAZIONE ZIP = prende TUTTO)
#  Motivo: alcuni EPUB referenziano asset non dichiarati nel manifest OPF.
#  ebooklib.get_items() NON li vede -> mancavano font/immagini -> ERR_FILE_NOT_FOUND.
# =========================================================
def export_epub_folder_from_zip(epub_path: str, out_dir="epub_export"):
    os.makedirs(out_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(epub_path, "r") as z:
            z.extractall(out_dir)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"EPUB non valido/ZIP corrotto: {e}")
    return out_dir


# =========================================================
# FIX CSS: riscrive url(...) e @import (gestisce anche "/...") + ripulisce ?#...
# =========================================================
_URL_RE = re.compile(r'url\(\s*(?P<q>["\']?)(?P<u>[^"\'\)]+)(?P=q)\s*\)', re.I)
_IMPORT_RE = re.compile(r'@import\s+(?:url\(\s*)?(?P<q>["\']?)(?P<u>[^"\'\)]+)(?P=q)\s*\)?', re.I)


def _clean_ref(u: str) -> str:
    u = (u or "").strip().replace("\\", "/")
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u


def _is_external(u: str) -> bool:
    if not u:
        return True
    lu = u.lower()
    return lu.startswith(("http://", "https://", "data:", "mailto:", "tel:", "#"))


def fix_css_file(css_abs_path: str, css_rel_path_posix: str):
    """
    css_rel_path_posix: path POSIX relativo a out_dir, es: "styles/stylesheet.css"
    """
    css_dir = posixpath.dirname(css_rel_path_posix)  # es: "styles" oppure ""

    raw = open(css_abs_path, "rb").read()
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        txt = raw.decode("utf-8", "ignore")

    def repl_url(m):
        q = m.group("q") or ""
        u = m.group("u")
        if _is_external(u):
            return m.group(0)

        u2 = _clean_ref(u)

        # root EPUB: "/fonts/x.ttf" -> relativo alla cartella del CSS
        if u2.startswith("/"):
            target = u2.lstrip("/")
            newu = posixpath.relpath(target, css_dir or ".")
            return f"url({q}{newu}{q})"

        return f"url({q}{u2}{q})"

    def repl_import(m):
        q = m.group("q") or ""
        u = m.group("u")
        if _is_external(u):
            return m.group(0)

        u2 = _clean_ref(u)

        if u2.startswith("/"):
            target = u2.lstrip("/")
            newu = posixpath.relpath(target, css_dir or ".")
            return f"@import {q}{newu}{q}"

        return f"@import {q}{u2}{q}"

    new_txt = _URL_RE.sub(repl_url, txt)
    new_txt = _IMPORT_RE.sub(repl_import, new_txt)

    if new_txt != txt:
        with open(css_abs_path, "w", encoding="utf-8") as f:
            f.write(new_txt)


def fix_all_css_under(out_dir: str):
    for root, _, files in os.walk(out_dir):
        for fn in files:
            if fn.lower().endswith(".css"):
                abs_path = os.path.join(root, fn)
                rel_path = os.path.relpath(abs_path, out_dir).replace(os.sep, "/")
                fix_css_file(abs_path, rel_path)


# =========================================================
# EPUB: crea full.html (NO traduzione)
# =========================================================
def make_full_html(book, out_dir="epub_export", out_name="full.html"):
    os.makedirs(out_dir, exist_ok=True)

    spine_ids = [idref for (idref, _) in book.spine if idref not in ("nav", "ncx")]

    css_links = []
    inline_styles = []
    body_parts = []

    html_attrs = {}
    html_class_order = []
    body_attrs = {}
    body_class_order = []

    def add_class(order_list, cls_string):
        if not cls_string:
            return
        for c in cls_string.split():
            if c not in order_list:
                order_list.append(c)

    def is_relative(url):
        if not url:
            return False
        u = url.strip().lower()
        return not (u.startswith(("http://", "https://", "mailto:", "tel:", "data:", "#")) or u.startswith("/"))

    def normalize_ref(v, base_dir):
        if not v:
            return v
        v = v.strip()
        if not v:
            return v
        lv = v.lower()
        if lv.startswith(("http://", "https://", "mailto:", "tel:", "data:", "#")):
            return v
        if v.startswith("/"):
            return v.lstrip("/")
        if is_relative(v):
            return posixpath.normpath(posixpath.join(base_dir, v))
        return v

    def fix_all_urls(doc, base_dir):
        for el in doc.iter():
            if "src" in el.attrib:
                nv = normalize_ref(el.attrib.get("src"), base_dir)
                if nv:
                    el.attrib["src"] = nv

            if "href" in el.attrib:
                nv = normalize_ref(el.attrib.get("href"), base_dir)
                if nv:
                    el.attrib["href"] = nv

            for k in list(el.attrib.keys()):
                if k == "xlink:href" or k.endswith("}href"):
                    nv = normalize_ref(el.attrib.get(k), base_dir)
                    if nv:
                        el.attrib[k] = nv

    def add_css_link(href, base_dir=""):
        if not href:
            return
        h = href.strip()
        if not h:
            return
        if h.startswith("/"):
            h = h.lstrip("/")
        elif is_relative(h) and base_dir:
            h = posixpath.normpath(posixpath.join(base_dir, h))
        css_links.append(h)

    # 1) CSS dal manifest (sempre)
    for css_item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        name = css_item.get_name()
        if name:
            css_links.append(name)

    # 2) Unisci capitoli
    for idref in spine_ids:
        item = book.get_item_with_id(idref)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        chapter_path = item.get_name() or ""
        base_dir = posixpath.dirname(chapter_path)

        doc = lxml_html.fromstring(item.get_content())

        # preserva <html>
        html_nodes = doc.xpath("//*[local-name()='html']")
        if html_nodes:
            hnode = html_nodes[0]
            add_class(html_class_order, hnode.get("class"))
            if not html_attrs:
                for k in ("lang", "dir", "xml:lang"):
                    v = hnode.get(k)
                    if v:
                        html_attrs[k] = v

        # link stylesheet nel capitolo
        for link in doc.xpath(
            "//*[local-name()='link' and translate(@rel,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='stylesheet']"
        ):
            add_css_link(link.get("href"), base_dir=base_dir)

        # style inline
        for style in doc.xpath("//*[local-name()='style']"):
            css = (style.text or "").strip()
            if css:
                inline_styles.append(css)

        # fix URL risorse
        fix_all_urls(doc, base_dir)

        bodies = doc.xpath("//*[local-name()='body']")
        if bodies:
            body = bodies[0]

            add_class(body_class_order, body.get("class"))

            if not body_attrs:
                for k in ("style", "dir", "lang", "id"):
                    v = body.get(k)
                    if v:
                        body_attrs[k] = v

            parts = []
            if body.text and body.text.strip():
                parts.append(pyhtml.escape(body.text))

            parts.append("".join(lxml_html.tostring(child, encoding="unicode") for child in body))
            body_parts.append("\n".join(parts))

    # dedup CSS
    seen = set()
    css_links_unique = []
    for c in css_links:
        if c and c not in seen:
            seen.add(c)
            css_links_unique.append(c)

    head_css = "\n".join(f'<link rel="stylesheet" href="{c}">' for c in css_links_unique)

    head_inline = ""
    if inline_styles:
        head_inline = "<style>\n" + "\n\n".join(inline_styles) + "\n</style>"

    if html_class_order:
        html_attrs["class"] = " ".join(html_class_order)
    if body_class_order:
        body_attrs["class"] = " ".join(body_class_order)

    html_attr_str = ""
    if html_attrs:
        html_attr_str = " " + " ".join(f'{k}="{pyhtml.escape(v, quote=True)}"' for k, v in html_attrs.items())

    body_attr_str = ""
    if body_attrs:
        body_attr_str = " " + " ".join(
            f'{k}="{pyhtml.escape(v, quote=True)}"' for k, v in body_attrs.items()
        )


    # ^^^ Nota: la riga sopra evita un raro problema se qualcuno usa chiavi strane.
    # Se preferisci "pulito", puoi sostituirla con:
    # body_attr_str = " " + " ".join(f'{k}="{pyhtml.escape(v, quote=True)}"' for k, v in body_attrs.items())

    full = f"""<!doctype html>
<html{html_attr_str}>
<head>
<meta charset="utf-8">
<title>EPUB(unito)</title>
{head_css}
{head_inline}
</head>
<body{body_attr_str}>
{"<hr>".join(body_parts)}
</body>
</html>"""

    out_path = os.path.join(out_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full)

    return out_path


# =========================================================
# MOSSE DI SCACCHI: riconoscimento + mascheramento
# =========================================================
CHESS_MOVE_TOKEN = re.compile(
    r"(?:\bO-O-O\b|\bO-O\b|"
    r"\b\d+\.(?:\.\.)?\s*(?:O-O-O|O-O|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?|[a-h]x?[a-h][1-8](?:=[QRBN])?[+#]?)|"
    r"\b[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?\b|"
    r"\b[a-h]x?[a-h][1-8](?:=[QRBN])?[+#]?\b|"
    r"\b(1-0|0-1|1/2-1/2|\*)\b)"
)

MOVE_NUM = re.compile(r"^\d+\.(?:\.\.)?$|^\d+\.\.\.$")
RESULT_TOK = re.compile(r"^(1-0|0-1|1/2-1/2|\*)$")


def looks_like_chess_line(text):
    """True se la riga è 'quasi tutta' fatta di mosse/annotazioni."""
    t = (text or "").strip()
    if not t:
        return False
    toks = re.split(r"\s+", t)
    if len(toks) < 3:
        return False

    chess = 0
    for tok in toks:
        x = tok.strip().strip(",;")
        if MOVE_NUM.match(x) or RESULT_TOK.match(x):
            chess += 1
            continue
        if CHESS_MOVE_TOKEN.fullmatch(x):
            chess += 1

    return (chess / len(toks)) >= 0.60


def mask_chess_moves_in_text(text):
    mapping = {}
    i = 0

    def repl(m):
        nonlocal i
        key = f"⟬MV{i}⟭"
        mapping[key] = m.group(0)
        i += 1
        return key

    masked = CHESS_MOVE_TOKEN.sub(repl, text)
    return masked, mapping


def unmask_chess_moves(text, mapping):
    out = text
    for k, v in mapping.items():
        out = re.sub(
            rf"(\s*){re.escape(k)}(\s*)",
            lambda m: f"{m.group(1)}{v}{m.group(2)}",
            out
        )
    return out


# =========================================================
# TRADUZIONE VELOCE + TAG INTACTI
# =========================================================
SKIP_TAGS = {"script", "style", "head", "title", "meta", "link", "code", "pre", "kbd", "samp"}
LETTER_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")


def has_letters(s):
    return bool(s and LETTER_RE.search(s))


def split_ws(s):
    s = s or ""
    pre = re.match(r"^\s*", s).group(0)
    suf = re.search(r"\s*$", s).group(0)
    core = s.strip()
    return pre, core, suf


def collect_text_slots_in_block(block_el):
    slots = []

    def rec(node):
        if not isinstance(node.tag, str):
            return
        tag = node.tag.lower()
        if tag in SKIP_TAGS:
            return

        if node.text is not None:
            slots.append((node, "text", node.text))

        for child in node:
            rec(child)
            if isinstance(child.tag, str) and child.tag.lower() not in SKIP_TAGS:
                if child.tail is not None:
                    slots.append((child, "tail", child.tail))

    rec(block_el)
    return slots


async def translate_one_block_preserve_markup(block_el, lang, max_payload_chars, stop_event):
    if stop_event.is_set():
        return "CANCEL"

    slots_all = collect_text_slots_in_block(block_el)

    slots = []
    prefixes = []
    suffixes = []
    cores = []
    move_maps = []

    for el, attr, original in slots_all:
        if not original:
            continue
        if not has_letters(original):
            continue

        pre, core, suf = split_ws(original)
        if not core:
            continue

        if looks_like_chess_line(core):
            continue

        core_masked, mv_map = mask_chess_moves_in_text(core)

        slots.append((el, attr))
        prefixes.append(pre)
        suffixes.append(suf)
        cores.append(core_masked)
        move_maps.append(mv_map)

    if not slots:
        return "OK"

    delim = f"␞{uuid.uuid4().hex}␞"

    chunks = []
    current = []
    current_len = 0
    for c in cores:
        add_len = len(c) + (len(delim) if current else 0)
        if current and (current_len + add_len) > max_payload_chars:
            chunks.append(current)
            current = [c]
            current_len = len(c)
        else:
            current.append(c)
            current_len += add_len
    if current:
        chunks.append(current)

    out_parts = []

    for chunk in chunks:
        if stop_event.is_set():
            return "CANCEL"

        payload = delim.join(chunk)
        translated = await translate_text(payload, target_lang=lang)

        parts = re.split(rf"\s*{re.escape(delim)}\s*", translated)

        if len(parts) != len(chunk):
            parts = []
            for c in chunk:
                if stop_event.is_set():
                    return "CANCEL"
                parts.append(await translate_text(c, target_lang=lang))

        out_parts.extend(parts)

    if len(out_parts) != len(slots):
        out_parts = []
        for c in cores:
            if stop_event.is_set():
                return "CANCEL"
            out_parts.append(await translate_text(c, target_lang=lang))

    for i, (el, attr) in enumerate(slots):
        t = unmask_chess_moves(out_parts[i], move_maps[i])
        setattr(el, attr, f"{prefixes[i]}{t}{suffixes[i]}")

    return "OK"


async def translate_full_html_blocks(in_path, out_path, lang, block_xpath,
                                    max_payload_chars, throttle_s, progress_cb, stop_event):
    with open(in_path, "r", encoding="utf-8") as f:
        src = f.read()

    doc = lxml_html.fromstring(src)
    blocks = doc.xpath(block_xpath)

    total = len(blocks)
    done = 0

    for b in blocks:
        if stop_event.is_set():
            raise RuntimeError("Annullato")

        tag = b.tag.lower() if isinstance(b.tag, str) else ""
        if tag in SKIP_TAGS:
            done += 1
            if progress_cb:
                progress_cb(done, total, f"Skip <{tag}>")
            continue

        res = await translate_one_block_preserve_markup(b, lang, max_payload_chars, stop_event)
        if res == "CANCEL":
            raise RuntimeError("Annullato")

        done += 1
        if progress_cb:
            progress_cb(done, total, f"Traduzione <{tag}>")

        if throttle_s > 0:
            await asyncio.sleep(throttle_s)

    out_html = "<!doctype html>\n" + lxml_html.tostring(doc, encoding="unicode", method="html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out_html)

    return out_path


# =========================================================
# GUI
# =========================================================
def create_gui():
    root = tk.Tk()
    root.title("EPUB → HTML + Traduzione (semplice)")
    root.geometry("900x560")

    state = {
        "epub_path": None,
        "out_dir": os.path.abspath("epub_export"),
        "out_file": None,
        "worker": None,
        "queue": queue.Queue(),
        "stop_event": threading.Event(),
    }

    var_file = tk.StringVar(value="Nessun file selezionato.")
    var_out = tk.StringVar(value=state["out_dir"])
    var_lang = tk.StringVar(value="it")

    var_export = tk.BooleanVar(value=True)
    var_make_full = tk.BooleanVar(value=True)
    var_translate = tk.BooleanVar(value=True)

    var_p = tk.BooleanVar(value=True)
    var_li = tk.BooleanVar(value=True)
    var_h = tk.BooleanVar(value=True)
    var_table = tk.BooleanVar(value=False)
    var_caption = tk.BooleanVar(value=False)
    var_quote = tk.BooleanVar(value=False)

    var_speed = tk.IntVar(value=3500)
    var_throttle = tk.IntVar(value=20)

    var_status = tk.StringVar(value="Pronto.")
    var_pct = tk.StringVar(value="0%")

    def log_line(s):
        log.configure(state="normal")
        log.insert("end", s + "\n")
        log.see("end")
        log.configure(state="disabled")

    def choose_epub():
        path = filedialog.askopenfilename(
            title="Seleziona EPUB",
            filetypes=[("EPUB", "*.epub"), ("Tutti i file", "*.*")]
        )
        if not path:
            return
        state["epub_path"] = path
        var_file.set(path)
        btn_start.configure(state="normal")
        log_line("Selezionato: " + path)

    def choose_out_dir():
        path = filedialog.askdirectory(title="Scegli cartella output")
        if not path:
            return
        state["out_dir"] = path
        var_out.set(path)
        log_line("Output: " + path)

    def open_output_folder():
        if not state.get("out_dir"):
            return
        try:
            os.startfile(state["out_dir"])  # Windows
        except Exception:
            messagebox.showinfo("Output", state["out_dir"])

    def set_ui_running(running):
        if running:
            btn_start.configure(state="disabled")
            btn_cancel.configure(state="normal")
            btn_pick.configure(state="disabled")
            btn_out.configure(state="disabled")
        else:
            btn_start.configure(state="normal" if state["epub_path"] else "disabled")
            btn_cancel.configure(state="disabled")
            btn_pick.configure(state="normal")
            btn_out.configure(state="normal")

    def progress_cb(done, total, msg):
        state["queue"].put(("PROGRESS", done, total, msg))

    def build_block_xpath():
        tags = []
        if var_p.get(): tags.append("self::p")
        if var_li.get(): tags.append("self::li")
        if var_h.get():
            tags += ["self::h1", "self::h2", "self::h3", "self::h4", "self::h5", "self::h6"]
        if var_table.get(): tags += ["self::td", "self::th"]
        if var_caption.get(): tags.append("self::figcaption")
        if var_quote.get(): tags.append("self::blockquote")

        if not tags:
            tags = ["self::p"]

        return "//body//*[" + " or ".join(tags) + "]"

    def worker():
        try:
            state["queue"].put(("STATUS", "Lettura EPUB…"))
            book = epub.read_epub(state["epub_path"])

            out_dir = state["out_dir"]
            os.makedirs(out_dir, exist_ok=True)

            if state["stop_event"].is_set():
                raise RuntimeError("Annullato")

            # Se vuoi full.html o traduzione, servono asset (font/immagini/css): estraiamo comunque
            must_have_assets = bool(var_make_full.get() or var_translate.get())
            do_export = bool(var_export.get() or must_have_assets)

            if not var_export.get() and must_have_assets:
                state["queue"].put(("LOG", "Nota: per applicare CSS/font è necessaria l'estrazione asset. Procedo comunque."))

            if do_export:
                state["queue"].put(("STATUS", "Estrazione EPUB (ZIP) + fix CSS…"))
                export_epub_folder_from_zip(state["epub_path"], out_dir=out_dir)
                fix_all_css_under(out_dir)
                state["queue"].put(("LOG", "Estrazione completata + CSS sistemati (url/@import)."))

            if state["stop_event"].is_set():
                raise RuntimeError("Annullato")

            full_path = os.path.join(out_dir, "full.html")
            if var_make_full.get() or var_translate.get():
                state["queue"].put(("STATUS", "Creazione full.html…"))
                full_path = make_full_html(book, out_dir=out_dir, out_name="full.html")
                state["queue"].put(("LOG", "Creato: " + os.path.abspath(full_path)))

            if state["stop_event"].is_set():
                raise RuntimeError("Annullato")

            if var_translate.get():
                lang = (var_lang.get() or "it").strip()
                out_path = os.path.join(out_dir, f"full_{lang}.html")
                block_xpath = build_block_xpath()
                max_payload_chars = int(var_speed.get())
                throttle_s = max(0, int(var_throttle.get())) / 1000.0

                state["queue"].put(("STATUS", f"Traduzione in '{lang}'… (mosse escluse)"))

                out_path = asyncio.run(
                    translate_full_html_blocks(
                        in_path=full_path,
                        out_path=out_path,
                        lang=lang,
                        block_xpath=block_xpath,
                        max_payload_chars=max_payload_chars,
                        throttle_s=throttle_s,
                        progress_cb=progress_cb,
                        stop_event=state["stop_event"]
                    )
                )
                state["out_file"] = out_path
                state["queue"].put(("DONE", os.path.abspath(out_path)))
            else:
                state["out_file"] = full_path
                state["queue"].put(("DONE", os.path.abspath(full_path)))

        except Exception as e:
            state["queue"].put(("ERROR", str(e)))

    def start():
        if not state["epub_path"]:
            return

        state["stop_event"].clear()
        bar["value"] = 0
        var_pct.set("0%")
        var_status.set("Avvio…")
        log_line("== Avvio ==")

        set_ui_running(True)

        t = threading.Thread(target=worker, daemon=True)
        state["worker"] = t
        t.start()

    def cancel():
        state["stop_event"].set()
        var_status.set("Annullamento richiesto…")
        log_line("Richiesto annullamento…")

    def poll_queue():
        try:
            while True:
                item = state["queue"].get_nowait()
                kind = item[0]

                if kind == "STATUS":
                    var_status.set(item[1])
                    log_line(item[1])

                elif kind == "LOG":
                    log_line(item[1])

                elif kind == "PROGRESS":
                    _, done, total, msg = item
                    pct = (done / total) * 100 if total else 0
                    bar["value"] = pct
                    var_pct.set(f"{pct:.1f}%")
                    var_status.set(f"{msg}: {done}/{total} ({pct:.1f}%)")

                elif kind == "DONE":
                    outp = item[1]
                    bar["value"] = 100
                    var_pct.set("100%")
                    var_status.set("Finito!")
                    log_line("FINITO: " + outp)
                    set_ui_running(False)

                elif kind == "ERROR":
                    err = item[1]
                    var_status.set("Errore.")
                    log_line("ERRORE: " + err)
                    set_ui_running(False)
                    messagebox.showerror("Errore", err)

        except queue.Empty:
            pass

        root.after(120, poll_queue)

    # UI
    frm_top = ttk.Frame(root)
    frm_top.pack(fill="x", padx=12, pady=10)

    ttk.Label(frm_top, text="EPUB:").grid(row=0, column=0, sticky="w")
    ttk.Label(frm_top, textvariable=var_file).grid(row=0, column=1, sticky="we", padx=8)
    btn_pick = ttk.Button(frm_top, text="Seleziona…", command=choose_epub)
    btn_pick.grid(row=0, column=2)

    ttk.Label(frm_top, text="Output:").grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Label(frm_top, textvariable=var_out).grid(row=1, column=1, sticky="we", padx=8, pady=(8, 0))
    btn_out = ttk.Button(frm_top, text="Cartella…", command=choose_out_dir)
    btn_out.grid(row=1, column=2, pady=(8, 0))

    frm_top.columnconfigure(1, weight=1)

    frm_opts = ttk.LabelFrame(root, text="Cosa fare")
    frm_opts.pack(fill="x", padx=12, pady=(0, 10))

    ttk.Checkbutton(frm_opts, text="Estrai risorse (immagini/css/font)", variable=var_export)\
        .grid(row=0, column=0, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_opts, text="Crea full.html", variable=var_make_full)\
        .grid(row=0, column=1, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_opts, text="Traduci", variable=var_translate)\
        .grid(row=0, column=2, sticky="w", padx=8, pady=4)

    ttk.Label(frm_opts, text="Lingua (es: it, en, fr):")\
        .grid(row=1, column=0, sticky="w", padx=8, pady=(6, 4))
    ttk.Entry(frm_opts, textvariable=var_lang, width=8)\
        .grid(row=1, column=1, sticky="w", padx=8, pady=(6, 4))

    frm_blocks = ttk.LabelFrame(root, text="Blocchi da tradurre (meno blocchi = più veloce)")
    frm_blocks.pack(fill="x", padx=12, pady=(0, 10))

    ttk.Checkbutton(frm_blocks, text="<p> paragrafi", variable=var_p)\
        .grid(row=0, column=0, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="<li> elenchi", variable=var_li)\
        .grid(row=0, column=1, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Titoli (h1..h6)", variable=var_h)\
        .grid(row=0, column=2, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Tabelle (td/th)", variable=var_table)\
        .grid(row=0, column=3, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Figcaption", variable=var_caption)\
        .grid(row=1, column=0, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Blockquote", variable=var_quote)\
        .grid(row=1, column=1, sticky="w", padx=8, pady=4)

    frm_speed = ttk.LabelFrame(root, text="Velocità / stabilità")
    frm_speed.pack(fill="x", padx=12, pady=(0, 10))

    ttk.Label(frm_speed, text="Max payload (caratteri per richiesta):")\
        .grid(row=0, column=0, sticky="w", padx=8, pady=4)
    ttk.Scale(frm_speed, from_=1200, to=6500, orient="horizontal", variable=var_speed)\
        .grid(row=0, column=1, sticky="we", padx=8, pady=4)
    ttk.Label(frm_speed, textvariable=var_speed, width=6)\
        .grid(row=0, column=2, sticky="w", padx=8, pady=4)

    ttk.Label(frm_speed, text="Pausa tra richieste (ms):")\
        .grid(row=1, column=0, sticky="w", padx=8, pady=4)
    ttk.Scale(frm_speed, from_=0, to=120, orient="horizontal", variable=var_throttle)\
        .grid(row=1, column=1, sticky="we", padx=8, pady=4)
    ttk.Label(frm_speed, textvariable=var_throttle, width=6)\
        .grid(row=1, column=2, sticky="w", padx=8, pady=4)

    frm_speed.columnconfigure(1, weight=1)

    frm_run = ttk.Frame(root)
    frm_run.pack(fill="x", padx=12, pady=(0, 8))

    btn_start = ttk.Button(frm_run, text="Avvia", command=start, state="disabled")
    btn_start.pack(side="left")

    btn_cancel = ttk.Button(frm_run, text="Annulla", command=cancel, state="disabled")
    btn_cancel.pack(side="left", padx=(8, 0))

    btn_open = ttk.Button(frm_run, text="Apri cartella output", command=open_output_folder)
    btn_open.pack(side="left", padx=(8, 0))

    ttk.Label(frm_run, textvariable=var_status).pack(side="left", padx=(16, 0))
    ttk.Label(frm_run, textvariable=var_pct).pack(side="right")

    bar = ttk.Progressbar(root, orient="horizontal", mode="determinate", maximum=100)
    bar.pack(fill="x", padx=12, pady=(0, 10))

    ttk.Label(root, text="Log:").pack(anchor="w", padx=12)
    log = ScrolledText(root, height=10)
    log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    log.configure(state="disabled")

    root.after(120, poll_queue)
    root.mainloop()


def main():
    create_gui()


if __name__ == "__main__":
    main()
