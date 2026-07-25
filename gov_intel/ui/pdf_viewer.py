"""A reusable, embeddable PDF viewer widget with highlighting, annotations, and advanced search.
Powered by pypdfium2 for permissive, AGPL-free rendering."""

from __future__ import annotations

import fnmatch
import re
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Callable

import pypdfium2 as pdfium
from PIL import Image, ImageTk

KeywordRulesCallback = Callable[[], dict]

_COLOR_BY_LABEL = {
    "Red 🟥": (1, 0.2, 0.2),
    "Green 🟩": (0.2, 0.9, 0.2),
    "Blue 🟦": (0.3, 0.6, 1),
}
_DEFAULT_COLOR = (1, 1, 0)  # Yellow


def _rgb_to_badge(rgb: tuple[float, float, float] | list[float]) -> str:
    """Returns a matching visual badge for sidebar highlights index."""
    if not rgb or len(rgb) < 3:
        return "🟨"
    r, g, b = rgb[0], rgb[1], rgb[2]
    if r > 0.8 and g > 0.7:
        return "🟨"
    elif r > 0.8 and g < 0.5 and b < 0.5:
        return "🟥"
    elif g > 0.7 and r < 0.5:
        return "🟩"
    elif b > 0.8 and r < 0.5:
        return "🟦"
    elif r > 0.6 and b > 0.6:
        return "🟪"
    elif r > 0.8 and g > 0.5:
        return "🟧"
    elif g > 0.7 and b > 0.7:
        return "🌐"
    elif r > 0.8 and b > 0.6:
        return "🩷"
    return "🟨"


def _rgb_to_hex(rgb: tuple[float, float, float] | list[float]) -> str:
    """Convert a 0.0-1.0 RGB tuple to a Tkinter hex color string."""
    r, g, b = [int(min(max(c, 0.0), 1.0) * 255) for c in rgb]
    return f"#{r:02x}{g:02x}{b:02x}"


class PDFViewerWidget(ttk.Frame):
    """Scrollable, zoomable PDF renderer with interactive canvas overlay, search highlighting, and TOC."""

    def __init__(self, parent: tk.Widget, get_kw_rules_cb: KeywordRulesCallback):
        super().__init__(parent)
        self.get_kw_rules_cb = get_kw_rules_cb
        self.doc_obj: pdfium.PdfDocument | None = None
        self.pdf_path = ""
        self.zoom = 1.3
        self.highlight_color = _DEFAULT_COLOR
        self.pen_active = tk.BooleanVar(value=False)
        self.images: list[ImageTk.PhotoImage] = []
        self.page_offsets: list[dict] = []
        
        self.selected_text = ""
        self.selected_page_num = 0
        self.search_matches: list[dict] = []
        self.current_match_idx = -1
        self.drag_start: tuple[float, float] | None = None
        
        # Store UI-level annotations (highlights and notes)
        self.custom_highlights: list[dict] = []
        self.custom_notes: list[dict] = []

        # Search Modifiers
        self.use_regex = tk.BooleanVar(value=False)
        self.match_case = tk.BooleanVar(value=False)

        self._build_toolbar_row1()
        self._build_toolbar_row2()
        self._build_canvas_area()
        self._bind_keyboard_shortcuts()

    def _build_toolbar_row1(self) -> None:
        tb1 = ttk.Frame(self, padding=4)
        tb1.pack(fill="x", side="top")
        ttk.Checkbutton(tb1, text="🖍️ Pen", variable=self.pen_active).pack(side="left", padx=4)

        self.color_cb = ttk.Combobox(
            tb1,
            values=list(_COLOR_BY_LABEL.keys()) + ["Yellow 🟨"],
            state="readonly",
            width=10,
        )
        self.color_cb.pack(side="left", padx=2)
        self.color_cb.set("Yellow 🟨")
        self.color_cb.bind("<<ComboboxSelected>>", self._change_color)

        ttk.Button(tb1, text="✨ Auto Highlight", command=self.auto_highlight).pack(side="left", padx=4)
        ttk.Button(tb1, text="📤 Export Notes", command=self.export_annotations_report).pack(side="left", padx=4)

        z_frame = ttk.Frame(tb1)
        z_frame.pack(side="right", padx=4)
        ttk.Button(z_frame, text="🔍 +", width=3, command=lambda: self._set_zoom(0.2)).pack(side="left")
        ttk.Button(z_frame, text="🔍 -", width=3, command=lambda: self._set_zoom(-0.2)).pack(side="left")
        self.z_lbl = ttk.Label(z_frame, text="130%", font=("Segoe UI", 9, "bold"))
        self.z_lbl.pack(side="left", padx=4)

    def _build_toolbar_row2(self) -> None:
        tb2 = ttk.Frame(self, padding=4)
        tb2.pack(fill="x", side="top")
        ttk.Label(tb2, text="Find in PDF:").pack(side="left", padx=(4, 2))
        self.e_find = ttk.Entry(tb2, width=16)
        self.e_find.pack(side="left", padx=2)
        self.e_find.bind("<Return>", lambda e: self.perform_in_doc_search(forward=True))

        ttk.Checkbutton(tb2, text="Regex", variable=self.use_regex).pack(side="left", padx=2)
        ttk.Checkbutton(tb2, text="Aa", variable=self.match_case).pack(side="left", padx=2)

        ttk.Button(tb2, text="Search", command=lambda: self.perform_in_doc_search(True)).pack(side="left", padx=2)
        ttk.Button(tb2, text="◀", width=2, command=lambda: self.navigate_search_match(-1)).pack(side="left")
        ttk.Button(tb2, text="▶", width=2, command=lambda: self.navigate_search_match(1)).pack(side="left")
        self.lbl_search_count = ttk.Label(tb2, text="")
        self.lbl_search_count.pack(side="left", padx=6)

        ttk.Button(tb2, text="📌 Copy w/ Ref", command=self.copy_selected_text_with_ref).pack(side="right", padx=2)
        ttk.Button(tb2, text="📋 Copy", command=self.copy_selected_text_to_clipboard).pack(side="right", padx=2)
        self.lbl_selection_status = ttk.Label(tb2, text="", font=("Segoe UI", 8, "italic"))
        self.lbl_selection_status.pack(side="right", padx=6)

    def _build_canvas_area(self) -> None:
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        self.sidebar_nb = ttk.Notebook(body, width=200)
        self.sidebar_nb.pack(side="left", fill="y")

        # Tab 1: Pages
        f_pages = ttk.Frame(self.sidebar_nb)
        self.sidebar_nb.add(f_pages, text="Pages")
        self.lb_page_index = tk.Listbox(f_pages, bg="#ffffff", exportselection=False)
        self.lb_page_index.pack(fill="both", expand=True)
        self.lb_page_index.bind("<<ListboxSelect>>", self._on_sidebar_page_select)

        # Tab 2: Outlines / TOC
        f_toc = ttk.Frame(self.sidebar_nb)
        self.sidebar_nb.add(f_toc, text="Outline")
        self.toc_tree = ttk.Treeview(f_toc, show="tree", selectmode="browse")
        self.toc_tree.pack(fill="both", expand=True)
        self.toc_tree.bind("<<TreeviewSelect>>", self._on_toc_select)

        # Tab 3: Highlights Navigation
        f_hl = ttk.Frame(self.sidebar_nb)
        self.sidebar_nb.add(f_hl, text="Highlights")
        self.lb_highlights = tk.Listbox(f_hl, bg="#ffffff", exportselection=False, font=("Segoe UI", 9))
        self.lb_highlights.pack(fill="both", expand=True)
        self.lb_highlights.bind("<<ListboxSelect>>", self._on_highlight_item_select)

        canvas_frame = ttk.Frame(body)
        canvas_frame.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_frame, bg="#525659")
        sb_y = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        sb_x = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)

        sb_x.pack(side="bottom", fill="x")
        sb_y.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Button-3>", self._on_right_click)

    def _bind_keyboard_shortcuts(self) -> None:
        self.bind_all("<Control-f>", lambda e: self.e_find.focus_set())

    def _on_mousewheel(self, event: tk.Event) -> None:
        if event.delta:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        elif event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")

    def _on_ctrl_mousewheel(self, event: tk.Event) -> None:
        if event.delta > 0:
            self._set_zoom(0.1)
        elif event.delta < 0:
            self._set_zoom(-0.1)

    def load_pdf(self, path: str) -> None:
        self.pdf_path = path
        self.doc_obj = pdfium.PdfDocument(path)
        self.search_matches = []
        self.current_match_idx = -1
        self.custom_highlights = []
        self.custom_notes = []
        self.lb_highlights.delete(0, tk.END)

        self.lb_page_index.delete(0, tk.END)
        for i in range(len(self.doc_obj)):
            self.lb_page_index.insert(tk.END, f"Page {i + 1}")

        for item in self.toc_tree.get_children():
            self.toc_tree.delete(item)
            
        try:
            for idx, item in enumerate(self.doc_obj.get_toc()):
                # PDFium page indexes are 0-based
                self.toc_tree.insert("", "end", iid=f"toc_{idx}_{item.page_index}", text=f"{item.title} (p.{item.page_index + 1})")
        except Exception:
            pass  # Document might not have a TOC

        self.render()

    def render(self) -> None:
        if not self.doc_obj:
            return

        self.canvas.delete("all")
        self.images.clear()
        self.page_offsets.clear()

        y_offset, max_width = 15, 100

        for page_num in range(len(self.doc_obj)):
            page = self.doc_obj[page_num]
            bitmap = page.render(scale=self.zoom)
            pil_img = bitmap.to_pil()
            img = ImageTk.PhotoImage(pil_img)
            self.images.append(img)

            self.canvas.create_image(15, y_offset, anchor="nw", image=img, tags=("pdf_page", f"p_{page_num}"))
            
            p_w, p_h = pil_img.width, pil_img.height
            orig_w, orig_h = page.get_size()
            
            max_width = max(max_width, p_w)
            self.page_offsets.append({
                "page_num": page_num,
                "y_start": y_offset,
                "y_end": y_offset + p_h,
                "orig_w": orig_w,
                "orig_h": orig_h
            })
            y_offset += p_h + 15

        # Draw our custom UI highlights over the rendered images
        for hl in self.custom_highlights:
            p_num = hl["page_num"]
            p_info = self.page_offsets[p_num]
            left, bottom, right, top = hl["rect"]
            
            # Map PDF coordinates (bottom-left origin) to Tkinter coordinates (top-left origin)
            x0 = 15 + (left * self.zoom)
            y0 = p_info["y_start"] + ((p_info["orig_h"] - top) * self.zoom)
            x1 = 15 + (right * self.zoom)
            y1 = p_info["y_start"] + ((p_info["orig_h"] - bottom) * self.zoom)
            
            hex_color = _rgb_to_hex(hl["color"])
            self.canvas.create_rectangle(
                x0, y0, x1, y1,
                fill=hex_color, outline=hex_color, stipple="gray50", tags="custom_highlight"
            )

        self.canvas.configure(scrollregion=(0, 0, max_width + 30, y_offset + 30))
        
        # Keep search matches visible if they exist
        if self.search_matches and self.current_match_idx >= 0:
            self._jump_to_current_search_match()

    def _on_sidebar_page_select(self, _event: tk.Event) -> None:
        sel = self.lb_page_index.curselection()
        if sel and self.page_offsets:
            self.scroll_to_page(sel[0])

    def _on_toc_select(self, _event: tk.Event) -> None:
        sel = self.toc_tree.selection()
        if sel:
            parts = sel[0].split("_")
            if len(parts) >= 3 and parts[-1].isdigit():
                p_num = int(parts[-1])
                self.scroll_to_page(p_num)

    def _on_highlight_item_select(self, _event: tk.Event) -> None:
        sel = self.lb_highlights.curselection()
        if sel and self.custom_highlights:
            idx = sel[0]
            item = self.custom_highlights[idx]
            p_info = self.page_offsets[item["page_num"]]
            
            # Y offset maps the PDF 'top' coordinate to the canvas top-down coordinate
            y_offset = (p_info["orig_h"] - item["rect"][3]) * self.zoom
            self.scroll_to_page(item["page_num"], y_offset)

    def scroll_to_page(self, page_num: int, y_offset_within_page: float = 0) -> None:
        if not (0 <= page_num < len(self.page_offsets)):
            return
        y_pos = self.page_offsets[page_num]["y_start"] + y_offset_within_page
        total_h = float(self.canvas.cget("scrollregion").split()[3])
        if total_h > 0:
            self.canvas.yview_moveto(max(0, (y_pos - 20) / total_h))

    def perform_in_doc_search(self, forward: bool = True) -> None:
        term = self.e_find.get().strip()
        if not term or not self.doc_obj:
            return
        self.search_matches = []
        flags = 0 if self.match_case.get() else re.IGNORECASE

        for p_num in range(len(self.doc_obj)):
            page = self.doc_obj[p_num]
            textpage = page.get_textpage()
            text = textpage.get_text_range()

            if self.use_regex.get():
                pattern = term
            else:
                pattern = re.escape(term)

            try:
                for m in re.finditer(pattern, text, flags=flags):
                    n_rects = textpage.count_rects(index=m.start(), count=m.end() - m.start())
                    rects = [textpage.get_rect(i) for i in range(n_rects)]
                    if rects:
                        self.search_matches.append({"page_num": p_num, "rect": rects[0]})
            except re.error:
                self.lbl_search_count.config(text="Invalid Regex")
                return

        if self.search_matches:
            self.current_match_idx = 0
            self.lbl_search_count.config(text=f"Match 1 of {len(self.search_matches)}")
            self._jump_to_current_search_match()
        else:
            self.lbl_search_count.config(text="No matches found")

    def navigate_search_match(self, delta: int) -> None:
        if not self.search_matches:
            return
        self.current_match_idx = (self.current_match_idx + delta) % len(self.search_matches)
        self.lbl_search_count.config(text=f"Match {self.current_match_idx + 1} of {len(self.search_matches)}")
        self._jump_to_current_search_match()

    def _jump_to_current_search_match(self) -> None:
        m = self.search_matches[self.current_match_idx]
        p_num = m["page_num"]
        left, bottom, right, top = m["rect"]

        p_info = self.page_offsets[p_num]
        
        # PDF coordinates are bottom-left origin; Tkinter is top-left origin
        box_x0 = 15 + (left * self.zoom)
        box_y0 = p_info["y_start"] + ((p_info["orig_h"] - top) * self.zoom)
        box_x1 = 15 + (right * self.zoom)
        box_y1 = p_info["y_start"] + ((p_info["orig_h"] - bottom) * self.zoom)

        self.scroll_to_page(p_num, (p_info["orig_h"] - top) * self.zoom)

        self.canvas.delete("search_match_box")
        self.canvas.create_rectangle(
            box_x0, box_y0, box_x1, box_y1,
            outline="#f59e0b", width=3, fill="#fef08a", stipple="gray50", tags="search_match_box"
        )

    def _on_drag_start(self, event: tk.Event) -> None:
        self.canvas.delete("selection_box")
        self.drag_start = (self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))

    def _on_drag_motion(self, event: tk.Event) -> None:
        if not self.drag_start or self.pen_active.get():
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        sx, sy = self.drag_start
        self.canvas.delete("selection_box")
        self.canvas.create_rectangle(sx, sy, cx, cy, outline="#3b82f6", width=1, dash=(3, 3), tags="selection_box")

    def _on_drag_end(self, event: tk.Event) -> None:
        self.canvas.delete("selection_box")
        if self.drag_start is None or not self.doc_obj:
            return
            
        canvas_ex, canvas_ey = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        sx, sy = self.drag_start

        for p_info in self.page_offsets:
            if not (p_info["y_start"] <= sy <= p_info["y_end"]):
                continue
            
            page_num = p_info["page_num"]
            page = self.doc_obj[page_num]
            textpage = page.get_textpage()

            # Map Tkinter canvas selection back to PDF coordinate space
            left = (min(sx, canvas_ex) - 15) / self.zoom
            right = (max(sx, canvas_ex) - 15) / self.zoom
            top = p_info["orig_h"] - ((min(sy, canvas_ey) - p_info["y_start"]) / self.zoom)
            bottom = p_info["orig_h"] - ((max(sy, canvas_ey) - p_info["y_start"]) / self.zoom)

            extracted = textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top).strip()

            if self.pen_active.get():
                if extracted:
                    self.custom_highlights.append({
                        "page_num": page_num,
                        "rect": (left, bottom, right, top),
                        "color": self.highlight_color,
                        "text": extracted[:32],
                        "cat": "Manual"
                    })
                    self.render()
            else:
                if extracted:
                    self.selected_text = extracted
                    self.selected_page_num = page_num + 1
                    self.lbl_selection_status.config(text=f"Selected {len(extracted)} chars (p. {self.selected_page_num})")
            break

    def copy_selected_text_to_clipboard(self) -> None:
        if self.selected_text:
            self.clipboard_clear()
            self.clipboard_append(self.selected_text)
            self.lbl_selection_status.config(text="Copied to clipboard!")
        else:
            messagebox.showwarning("No Text Selected", "Click and drag over text on any page first.")

    def copy_selected_text_with_ref(self) -> None:
        if self.selected_text:
            ref_str = f'"{self.selected_text}"\n\n— Source: {self.pdf_path.rsplit("/", 1)[-1]}, Page {self.selected_page_num}'
            self.clipboard_clear()
            self.clipboard_append(ref_str)
            self.lbl_selection_status.config(text="Copied citation!")

    def _on_right_click(self, event: tk.Event) -> None:
        if not self.doc_obj:
            return
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for p_info in self.page_offsets:
            if not (p_info["y_start"] <= cy <= p_info["y_end"]):
                continue
            note = simpledialog.askstring("Add Sticky Note", f"Enter note for Page {p_info['page_num'] + 1}:")
            if note:
                self.custom_notes.append({
                    "page_num": p_info["page_num"],
                    "note": note
                })
                messagebox.showinfo("Note Added", "Note saved to your exportable report.")
            break

    def export_annotations_report(self) -> None:
        if not self.doc_obj:
            return
        report = [f"📋 ANNOTATION REPORT: {self.pdf_path.rsplit('/', 1)[-1]}\n{'=' * 50}"]
        
        # Collate highlights and notes by page
        page_items = {}
        for hl in self.custom_highlights:
            page_items.setdefault(hl["page_num"], []).append(f"  ↳ [HIGHLIGHT - {hl.get('cat', 'Manual')}] {hl['text']}...")
        for note in self.custom_notes:
            page_items.setdefault(note["page_num"], []).append(f"  ↳ [STICKY NOTE] {note['note']}")

        for p_num in sorted(page_items.keys()):
            report.append(f"\n📌 PAGE {p_num + 1}:")
            report.extend(page_items[p_num])
            
        if not page_items:
            report.append("\nNo annotations or highlights created yet.")

        pop = tk.Toplevel(self)
        pop.title("📤 Annotations Export")
        pop.geometry("600x450")
        txt = scrolledtext.ScrolledText(pop, wrap="word")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert(tk.END, "\n".join(report))

    def _set_zoom(self, delta: float) -> None:
        self.zoom = round(max(0.5, min(3.0, self.zoom + delta)), 1)
        self.z_lbl.config(text=f"{int(self.zoom * 100)}%")
        self.render()

    def _change_color(self, _event: tk.Event) -> None:
        self.highlight_color = _COLOR_BY_LABEL.get(self.color_cb.get(), _DEFAULT_COLOR)

    def auto_highlight(self, show_dialog: bool = True) -> None:
        """Enhanced auto-highlight engine supporting multi-word phrases, line splits, and wildcards."""
        if not self.doc_obj:
            return
        rules = self.get_kw_rules_cb()
        count = 0
        self.custom_highlights = []
        self.lb_highlights.delete(0, tk.END)

        no_text_pages = []

        for p_num in range(len(self.doc_obj)):
            page = self.doc_obj[p_num]
            textpage = page.get_textpage()
            raw_p_text = textpage.get_text_range()

            if not raw_p_text.strip():
                no_text_pages.append(p_num + 1)
                continue

            text_lower = raw_p_text.lower()

            for cat, data in rules.items():
                raw_color = data.get("color", [1.0, 0.9, 0.2])
                clean_rgb = [val / 255.0 if val > 1.0 else float(val) for val in raw_color]
                cat_color = tuple(clean_rgb)
                badge = _rgb_to_badge(cat_color)
                
                active_terms = [t for t, enabled in data.get("terms", {}).items() if enabled]
                if not active_terms:
                    continue

                for term in active_terms:
                    term_clean = term.strip().lower()
                    if not term_clean:
                        continue

                    # Handle wildcards if present, otherwise exact match
                    if ("*" in term_clean or "?" in term_clean) and (" " not in term_clean):
                        pattern = fnmatch.translate(term_clean)
                    else:
                        pattern = re.escape(term_clean)

                    for m in re.finditer(pattern, text_lower):
                        n_rects = textpage.count_rects(index=m.start(), count=m.end() - m.start())
                        rects = [textpage.get_rect(i) for i in range(n_rects)]
                        if not rects:
                            continue
                            
                        # Use the first bounding box for the UI representation
                        rect = rects[0]
                        snippet = raw_p_text[max(0, m.start() - 10):m.end() + 20].replace('\n', ' ').strip()
                        
                        self.custom_highlights.append({
                            "page_num": p_num,
                            "rect": rect,
                            "color": cat_color,
                            "text": snippet,
                            "cat": cat
                        })
                        
                        snippet_label = f"{badge} P.{p_num + 1} [{cat[:10]}] {snippet[:32]}..."
                        self.lb_highlights.insert(tk.END, snippet_label)
                        count += 1

        if count > 0:
            self.render()
            self.sidebar_nb.select(2)

        if show_dialog:
            msg = f"Applied {count} translucent highlight(s) matching your categories!"
            if no_text_pages:
                msg += f"\n\n⚠️ Note: Page(s) {', '.join(map(str, no_text_pages[:5]))} contain no extractable text."
            messagebox.showinfo("Auto Highlight Results", msg)