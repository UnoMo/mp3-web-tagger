import base64
import os
from io import BytesIO
from pathlib import Path
from typing import Dict, Any, Optional

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, abort
)
from werkzeug.utils import secure_filename

from mutagen.id3 import (
    ID3, ID3NoHeaderError, ID3v2VersionError,
    TIT2, TPE1, TPE2, TALB, TCOM, TCON, TDRC, TYER, TRCK, TPOS,
    COMM, USLT, APIC
)
from mutagen.mp3 import MP3

# ---------------- Config ----------------

ALLOWED_MP3_EXTS = {".mp3"}
ALLOWED_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
COVER_TYPE_MAP = {"front": 3, "back": 4}

def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,  # 50MB limit
        UPLOAD_FOLDER=str(Path(app.instance_path) / "uploads"),
        SAVE_AS_V23=True,  # save as ID3v2.3 for max compatibility
        PREFERRED_URL_SCHEME="https",
    )

    # ensure folders
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # ------------ Helpers ------------

    def load_id3(file_path: Path) -> ID3:
        try:
            return ID3(file_path)
        except ID3NoHeaderError:
            return ID3()

    def save_id3(file_path: Path, tags: ID3) -> None:
        if app.config.get("SAVE_AS_V23", True):
            tags.save(file_path, v2_version=3)
        else:
            tags.save(file_path)

    def get_text(tags: ID3, key: str) -> str:
        f = tags.get(key)
        return (f.text[0] if (f and hasattr(f, "text") and f.text) else "")

    def get_common(tags: ID3) -> Dict[str, Any]:
        # comments / lyrics (English slot, empty desc)
        comment = ""
        for f in tags.getall("COMM"):
            if f.lang.lower() in ("eng", "en") and f.desc == "":
                comment = f.text or ""
                break

        lyrics = ""
        for f in tags.getall("USLT"):
            if f.lang.lower() in ("eng", "en") and f.desc == "":
                lyrics = f.text or ""
                break

        # date: prefer TDRC, fallback TYER
        date = get_text(tags, "TDRC") or get_text(tags, "TYER")

        return {
            "title":       get_text(tags, "TIT2"),
            "artist":      get_text(tags, "TPE1"),
            "album":       get_text(tags, "TALB"),
            "albumartist": get_text(tags, "TPE2"),
            "composer":    get_text(tags, "TCOM"),
            "genre":       get_text(tags, "TCON"),
            "date":        date,
            "track":       get_text(tags, "TRCK"),
            "disc":        get_text(tags, "TPOS"),
            "comment":     comment,
            "lyrics":      lyrics,
        }

    def set_field(tags: ID3, field: str, value: str) -> None:
        f = field.lower()
        if f == "title":
            tags["TIT2"] = TIT2(encoding=3, text=value)
        elif f == "artist":
            tags["TPE1"] = TPE1(encoding=3, text=value)
        elif f == "album":
            tags["TALB"] = TALB(encoding=3, text=value)
        elif f == "albumartist":
            tags["TPE2"] = TPE2(encoding=3, text=value)
        elif f == "composer":
            tags["TCOM"] = TCOM(encoding=3, text=value)
        elif f == "genre":
            tags["TCON"] = TCON(encoding=3, text=value)
        elif f == "date":
            tags["TDRC"] = TDRC(encoding=3, text=value)
            if value.isdigit() and len(value) == 4:
                tags["TYER"] = TYER(encoding=3, text=value)
        elif f == "track":
            tags["TRCK"] = TRCK(encoding=3, text=value)
        elif f == "disc":
            tags["TPOS"] = TPOS(encoding=3, text=value)
        elif f == "comment":
            tags.add(COMM(encoding=3, lang="eng", desc="", text=value))
        elif f == "lyrics":
            tags.add(USLT(encoding=3, lang="eng", desc="", text=value))

    def infer_mime(image_name: str) -> str:
        ext = Path(image_name).suffix.lower()
        return MIME_BY_EXT.get(ext, "image/jpeg")

    def get_cover_b64(tags: ID3, kind: str = "front") -> Optional[Dict[str, str]]:
        ctype = COVER_TYPE_MAP.get(kind, 3)
        for apic in tags.getall("APIC"):
            if apic.type == ctype:
                b64 = base64.b64encode(apic.data).decode("ascii")
                return {"mime": apic.mime, "data_url": f"data:{apic.mime};base64,{b64}"}
        return None

    def remove_cover(tags: ID3, kind: Optional[str]) -> int:
        if kind in (None, "all"):
            n = len(tags.getall("APIC"))
            tags.delall("APIC")
            return n
        ctype = COVER_TYPE_MAP.get(kind, 3)
        apics = tags.getall("APIC")
        tags.delall("APIC")
        kept = []
        removed = 0
        for apic in apics:
            if apic.type == ctype:
                removed += 1
            else:
                kept.append(apic)
        for k in kept:
            tags.add(k)
        return removed

    # --------------- Routes ---------------

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "POST":
            f = request.files.get("file")
            if not f or f.filename.strip() == "":
                flash("Please select an MP3 file.", "error")
                return redirect(url_for("index"))
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_MP3_EXTS:
                flash("Only .mp3 files are allowed.", "error")
                return redirect(url_for("index"))

            fname = secure_filename(f.filename)
            dest = Path(app.config["UPLOAD_FOLDER"]) / fname
            # overwrite for simplicity; you can also generate unique name
            f.save(dest)
            flash("File uploaded.", "ok")
            return redirect(url_for("edit", filename=fname))
        return render_template("index.html")

    @app.route("/edit/<path:filename>", methods=["GET"])
    def edit(filename):
        file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
        if not file_path.exists():
            abort(404)
        tags = load_id3(file_path)
        common = get_common(tags)
        cover_front = get_cover_b64(tags, "front")
        cover_back = get_cover_b64(tags, "back")
        return render_template(
            "edit.html",
            filename=filename,
            common=common,
            cover_front=cover_front,
            cover_back=cover_back
        )

    @app.post("/update/<path:filename>")
    def update(filename):
        file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
        if not file_path.exists():
            abort(404)
        tags = load_id3(file_path)

        fields = ["title","artist","album","albumartist","composer","genre","date","track","disc","comment","lyrics"]
        for fkey in fields:
            val = request.form.get(fkey, "")
            if val is not None:
                set_field(tags, fkey, val.strip())

        try:
            save_id3(file_path, tags)
        except ID3v2VersionError as e:
            flash(f"ID3 error: {e}", "error")
        else:
            flash("Tags saved.", "ok")
        return redirect(url_for("edit", filename=filename))

    @app.post("/cover/<path:filename>/add")
    def add_cover(filename):
        file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
        if not file_path.exists():
            abort(404)
        tags = load_id3(file_path)

        kind = request.form.get("kind", "front")
        img = request.files.get("image")
        if not img or img.filename.strip() == "":
            flash("Choose an image.", "error")
            return redirect(url_for("edit", filename=filename))

        ext = Path(img.filename).suffix.lower()
        if ext not in ALLOWED_IMG_EXTS:
            flash("Unsupported image type.", "error")
            return redirect(url_for("edit", filename=filename))

        data = img.read()
        mime = MIME_BY_EXT.get(ext, "image/jpeg")
        ctype = COVER_TYPE_MAP.get(kind, 3)

        # remove existing same type, then add
        apics = tags.getall("APIC")
        tags.delall("APIC")
        # keep others
        for apic in apics:
            if apic.type != ctype:
                tags.add(apic)
        tags.add(APIC(encoding=3, mime=mime, type=ctype, desc="", data=data))

        save_id3(file_path, tags)
        flash(f"{kind.capitalize()} cover updated.", "ok")
        return redirect(url_for("edit", filename=filename))

    @app.post("/cover/<path:filename>/remove")
    def remove_cover_route(filename):
        file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
        if not file_path.exists():
            abort(404)
        kind = request.form.get("kind")  # "front", "back", or "all"
        tags = load_id3(file_path)
        n = remove_cover(tags, kind)
        save_id3(file_path, tags)
        flash(f"Removed {n} cover image(s).", "ok")
        return redirect(url_for("edit", filename=filename))

    @app.get("/cover/<path:filename>/download")
    def download_cover(filename):
        # Download the front cover by default
        file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
        if not file_path.exists():
            abort(404)
        tags = load_id3(file_path)
        # try front first, then back
        for kind in ("front", "back"):
            ctype = COVER_TYPE_MAP[kind]
            for apic in tags.getall("APIC"):
                if apic.type == ctype:
                    return send_file(
                        BytesIO(apic.data),
                        mimetype=apic.mime or "application/octet-stream",
                        as_attachment=True,
                        download_name=f"{Path(filename).stem}-{kind}.jpg"
                    )
        flash("No cover image found to download.", "error")
        return redirect(url_for("edit", filename=filename))

    return app


app = create_app()

if __name__ == "__main__":
    # Local dev: flask run OR python app_main.py
    app.run(debug=True, host="0.0.0.0", port=5000)
