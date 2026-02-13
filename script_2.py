import os
import re
import uuid
import threading
import queue
import asyncio
import inspect

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

import ebooklib
from ebooklib import epub
from lxml import html
import posixpath

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
# EPUB: export risorse + crea full.html (NO traduzione)
# =========================================================
def export_epub_folder(book, out_dir="epub_export"):
    os.makedirs(out_dir, exist_ok=True)
    for item in book.get_items():
        name = item.get_name()
        path = os.path.join(out_dir, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(item.get_content())
    return out_dir

def is_relative(url):
    if not url:
        return False
    u = url.strip().lower()
    return not (u.startswith(("http://", "https://", "mailto:", "tel:", "data:", "#", "/")))

def make_full_html(book, out_dir="epub_export", out_name="full.html"):
    os.makedirs(out_dir, exist_ok=True)

    spine_ids = [idref for (idref, _) in book.spine if idref not in ("nav", "ncx")]

    css_links = []
    inline_styles = []
    body_parts = []

    for idref in spine_ids:
        item = book.get_item_with_id(idref)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        chapter_path = item.get_name()
        base_dir = posixpath.dirname(chapter_path)
        doc = html.fromstring(item.get_content())

        # CSS esterni
        for link in doc.xpath('//link[@rel="stylesheet"]'):
            href = link.get("href")
            if href and is_relative(href):
                href = posixpath.normpath(posixpath.join(base_dir, href))
            if href:
                css_links.append(href)

        # CSS inline
        for style in doc.xpath("//style"):
            css = (style.text or "").strip()
            if css:
                inline_styles.append(css)

        # Fix src
        for el in doc.xpath('//*[@src]'):
            src = el.get("src")
            if src and is_relative(src):
                el.set("src", posixpath.normpath(posixpath.join(base_dir, src)))

        # Fix href
        for el in doc.xpath('//*[@href]'):
            href = el.get("href")
            if href and is_relative(href):
                el.set("href", posixpath.normpath(posixpath.join(base_dir, href)))

        body = doc.find(".//body")
        if body is not None:
            inner = "\n".join(html.tostring(child, encoding="unicode") for child in body)
            body_parts.append(inner)

    # dedup CSS
    seen = set()
    css_links_unique = []
    for c in css_links:
        if c not in seen:
            seen.add(c)
            css_links_unique.append(c)

    head_css = "\n".join(f'<link rel="stylesheet" href="{c}">' for c in css_links_unique)

    head_inline = ""
    if inline_styles:
        head_inline = "<style>\n" + "\n\n".join(inline_styles) + "\n</style>"

    full = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>EPUB(unito)</title>
{head_css}
{head_inline}
</head>
<body>
{"<hr>".join(body_parts)}
</body>
</html>"""

    out_path = os.path.join(out_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full)

    return out_path


# =========================================================
# TRADUZIONE VELOCE (meno richieste) + TAG INTACTI
#  - 1 richiesta per "blocco" (p, li, h1..h6, td, ecc.)
#  - dentro il blocco traduce tanti pezzetti insieme con un delimitatore
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
    """
    Prende text/tail in ordine di rendering, ma non tocca i tag.
    """
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
    """
    Traduce SOLO il testo del blocco, senza rimuovere tag/attributi.
    """
    if stop_event.is_set():
        return "CANCEL"

    slots_all = collect_text_slots_in_block(block_el)

    slots = []
    prefixes = []
    suffixes = []
    cores = []

    for el, attr, original in slots_all:
        if not original:
            continue
        if not has_letters(original):
            continue

        pre, core, suf = split_ws(original)
        if not core:
            continue

        slots.append((el, attr))
        prefixes.append(pre)
        suffixes.append(suf)
        cores.append(core)

    if not slots:
        return "OK"

    delim = f"␞{uuid.uuid4().hex}␞"

    # chunking per non mandare payload enormi (più stabile)
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

        # se lo split non torna, fallback (solo per questo chunk)
        if len(parts) != len(chunk):
            parts = []
            for c in chunk:
                if stop_event.is_set():
                    return "CANCEL"
                parts.append(await translate_text(c, target_lang=lang))

        out_parts.extend(parts)

    # fallback estremo
    if len(out_parts) != len(slots):
        out_parts = []
        for c in cores:
            if stop_event.is_set():
                return "CANCEL"
            out_parts.append(await translate_text(c, target_lang=lang))

    for i, (el, attr) in enumerate(slots):
        setattr(el, attr, f"{prefixes[i]}{out_parts[i]}{suffixes[i]}")

    return "OK"

async def translate_full_html_blocks(in_path, out_path, lang, block_xpath, max_payload_chars, throttle_s, progress_cb, stop_event):
    with open(in_path, "r", encoding="utf-8") as f:
        src = f.read()

    doc = html.fromstring(src)
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

    out_html = "<!doctype html>\n" + html.tostring(doc, encoding="unicode", method="html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out_html)

    return out_path


# =========================================================
# GUI (solo funzioni)
# =========================================================
def create_gui():
    root = tk.Tk()
    root.title("EPUB → HTML + Traduzione (semplice)")
    root.geometry("900x560")

    # stato "semplice"
    state = {
        "epub_path": None,
        "out_dir": os.path.abspath("epub_export"),
        "out_file": None,
        "worker": None,
        "queue": queue.Queue(),
        "stop_event": threading.Event(),
    }

    # variabili UI
    var_file = tk.StringVar(value="Nessun file selezionato.")
    var_out = tk.StringVar(value=state["out_dir"])
    var_lang = tk.StringVar(value="it")

    var_export = tk.BooleanVar(value=True)
    var_make_full = tk.BooleanVar(value=True)
    var_translate = tk.BooleanVar(value=True)

    # quali blocchi tradurre
    var_p = tk.BooleanVar(value=True)
    var_li = tk.BooleanVar(value=True)
    var_h = tk.BooleanVar(value=True)
    var_table = tk.BooleanVar(value=False)
    var_caption = tk.BooleanVar(value=False)
    var_quote = tk.BooleanVar(value=False)

    var_speed = tk.IntVar(value=3500)     # max_payload_chars (più alto = meno richieste)
    var_throttle = tk.IntVar(value=20)    # ms di pausa (0..100). piccolo ma non zero.

    var_status = tk.StringVar(value="Pronto.")
    var_pct = tk.StringVar(value="0%")

    # --- funzioni helper UI ---
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

    # callback progress dal worker
    def progress_cb(done, total, msg):
        state["queue"].put(("PROGRESS", done, total, msg))

    def build_block_xpath():
        tags = []
        if var_p.get(): tags.append("self::p")
        if var_li.get(): tags.append("self::li")
        if var_h.get(): tags += ["self::h1","self::h2","self::h3","self::h4","self::h5","self::h6"]
        if var_table.get(): tags += ["self::td","self::th"]
        if var_caption.get(): tags.append("self::figcaption")
        if var_quote.get(): tags.append("self::blockquote")

        if not tags:
            # se l’utente disattiva tutto, almeno non esplode
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

            # esporta risorse
            if var_export.get():
                state["queue"].put(("STATUS", "Esportazione risorse…"))
                export_epub_folder(book, out_dir=out_dir)

            if state["stop_event"].is_set():
                raise RuntimeError("Annullato")

            # crea full.html
            full_path = os.path.join(out_dir, "full.html")
            if var_make_full.get() or var_translate.get():
                state["queue"].put(("STATUS", "Creazione full.html…"))
                full_path = make_full_html(book, out_dir=out_dir, out_name="full.html")
                state["queue"].put(("LOG", "Creato: " + os.path.abspath(full_path)))

            if state["stop_event"].is_set():
                raise RuntimeError("Annullato")

            # traduci
            if var_translate.get():
                lang = (var_lang.get() or "it").strip()
                out_path = os.path.join(out_dir, f"full_{lang}.html")
                block_xpath = build_block_xpath()
                max_payload_chars = int(var_speed.get())
                throttle_s = max(0, int(var_throttle.get())) / 1000.0

                state["queue"].put(("STATUS", f"Traduzione in '{lang}'… (pochi request)"))

                # async in thread
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

    # =====================================================
    # Layout GUI
    # =====================================================
    frm_top = ttk.Frame(root)
    frm_top.pack(fill="x", padx=12, pady=10)

    ttk.Label(frm_top, text="EPUB:").grid(row=0, column=0, sticky="w")
    ttk.Label(frm_top, textvariable=var_file).grid(row=0, column=1, sticky="we", padx=8)
    btn_pick = ttk.Button(frm_top, text="Seleziona…", command=choose_epub)
    btn_pick.grid(row=0, column=2)

    ttk.Label(frm_top, text="Output:").grid(row=1, column=0, sticky="w", pady=(8,0))
    ttk.Label(frm_top, textvariable=var_out).grid(row=1, column=1, sticky="we", padx=8, pady=(8,0))
    btn_out = ttk.Button(frm_top, text="Cartella…", command=choose_out_dir)
    btn_out.grid(row=1, column=2, pady=(8,0))

    frm_top.columnconfigure(1, weight=1)

    frm_opts = ttk.LabelFrame(root, text="Cosa fare")
    frm_opts.pack(fill="x", padx=12, pady=(0,10))

    ttk.Checkbutton(frm_opts, text="Esporta risorse (immagini/css)", variable=var_export).grid(row=0, column=0, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_opts, text="Crea full.html", variable=var_make_full).grid(row=0, column=1, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_opts, text="Traduci", variable=var_translate).grid(row=0, column=2, sticky="w", padx=8, pady=4)

    ttk.Label(frm_opts, text="Lingua (es: it, en, fr):").grid(row=1, column=0, sticky="w", padx=8, pady=(6,4))
    ttk.Entry(frm_opts, textvariable=var_lang, width=8).grid(row=1, column=1, sticky="w", padx=8, pady=(6,4))

    frm_blocks = ttk.LabelFrame(root, text="Blocchi da tradurre (meno blocchi = più veloce)")
    frm_blocks.pack(fill="x", padx=12, pady=(0,10))

    ttk.Checkbutton(frm_blocks, text="<p> paragrafi", variable=var_p).grid(row=0, column=0, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="<li> elenchi", variable=var_li).grid(row=0, column=1, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Titoli (h1..h6)", variable=var_h).grid(row=0, column=2, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Tabelle (td/th)", variable=var_table).grid(row=0, column=3, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Figcaption", variable=var_caption).grid(row=1, column=0, sticky="w", padx=8, pady=4)
    ttk.Checkbutton(frm_blocks, text="Blockquote", variable=var_quote).grid(row=1, column=1, sticky="w", padx=8, pady=4)

    frm_speed = ttk.LabelFrame(root, text="Velocità / stabilità")
    frm_speed.pack(fill="x", padx=12, pady=(0,10))

    ttk.Label(frm_speed, text="Max payload (caratteri per richiesta):").grid(row=0, column=0, sticky="w", padx=8, pady=4)
    ttk.Scale(frm_speed, from_=1200, to=6500, orient="horizontal", variable=var_speed).grid(row=0, column=1, sticky="we", padx=8, pady=4)
    ttk.Label(frm_speed, textvariable=var_speed, width=6).grid(row=0, column=2, sticky="w", padx=8, pady=4)

    ttk.Label(frm_speed, text="Pausa tra richieste (ms):").grid(row=1, column=0, sticky="w", padx=8, pady=4)
    ttk.Scale(frm_speed, from_=0, to=120, orient="horizontal", variable=var_throttle).grid(row=1, column=1, sticky="we", padx=8, pady=4)
    ttk.Label(frm_speed, textvariable=var_throttle, width=6).grid(row=1, column=2, sticky="w", padx=8, pady=4)

    frm_speed.columnconfigure(1, weight=1)

    frm_run = ttk.Frame(root)
    frm_run.pack(fill="x", padx=12, pady=(0,8))

    btn_start = ttk.Button(frm_run, text="Avvia", command=start, state="disabled")
    btn_start.pack(side="left")

    btn_cancel = ttk.Button(frm_run, text="Annulla", command=cancel, state="disabled")
    btn_cancel.pack(side="left", padx=(8,0))

    btn_open = ttk.Button(frm_run, text="Apri cartella output", command=open_output_folder)
    btn_open.pack(side="left", padx=(8,0))

    ttk.Label(frm_run, textvariable=var_status).pack(side="left", padx=(16,0))
    ttk.Label(frm_run, textvariable=var_pct).pack(side="right")

    bar = ttk.Progressbar(root, orient="horizontal", mode="determinate", maximum=100)
    bar.pack(fill="x", padx=12, pady=(0,10))

    ttk.Label(root, text="Log:").pack(anchor="w", padx=12)
    log = ScrolledText(root, height=10)
    log.pack(fill="both", expand=True, padx=12, pady=(0,12))
    log.configure(state="disabled")

    # avvia polling
    root.after(120, poll_queue)
    root.mainloop()


def main():
    create_gui()


if __name__ == "__main__":
    main()
