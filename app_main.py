import base64
import os
import zipfile
from io import BytesIO
from pathlib import Path
from functools import wraps
from typing import Dict, Any, Optional
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, send_from_directory, abort, Response
)

from werkzeug.utils import secure_filename

from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TPE2, TALB, TCOM, TCON, TDRC, TYER, TRCK, TPOS,
    COMM, USLT, APIC
)
from mutagen import MutagenError
# from mutagen.mp3 import MP3

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

    # --- Basic Auth setup ---
    AUTH_USER = os.environ.get("APP_USER", "")
    AUTH_PASS = os.environ.get("APP_PASS", "")

    def check_auth(username, password):
        return (username == AUTH_USER and password == AUTH_PASS)

    def auth_failed_response():
        # Triggers browser login dialog
        return Response(
            "Authentication required", 401,
            {"WWW-Authenticate": 'Basic realm="MP3 Tagger"'}
        )

    def require_auth(view_fn):
        @wraps(view_fn)
        def wrapped(*args, **kwargs):
            # If no creds configured, allow everything (so you don't lock yourself out by accident)
            if AUTH_USER == "" and AUTH_PASS == "":
                return view_fn(*args, **kwargs)

            auth = request.authorization
            if not auth or not check_auth(auth.username, auth.password):
                return auth_failed_response()
            return view_fn(*args, **kwargs)
        return wrapped

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,  # 50MB limit
        UPLOAD_FOLDER=str(Path(app.instance_path) / "uploads"),
        SAVE_AS_V23=True,  # save as ID3v2.3 for max compatibility
        PREFERRED_URL_SCHEME="https",
    )
    
    # App metadata / version
    app.config["APP_VERSION"] = os.environ.get("APP_VERSION", "v0.1-dev")

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
    
    def safe_get_title_artist(file_path: Path) -> dict:
        """
        Return {'title': ..., 'artist': ...} for display in Explore.
        Never throws; falls back to '' if unreadable.
        """
        try:
            tags = load_id3(file_path)
            meta = get_common(tags)
            return {
                "title": meta.get("title", "") or "",
                "artist": meta.get("artist", "") or "",
            }
        except Exception:
            return {"title": "", "artist": ""}

    def safe_get_front_cover_data_url(file_path: Path) -> str:
        """
        Return a base64 data URL for the front cover, or '' if none.
        We don't resize here; just use browser CSS to keep it small.
        """
        try:
            tags = load_id3(file_path)
            cover = get_cover_b64(tags, "front")
            if cover and "data_url" in cover:
                return cover["data_url"]
        except Exception:
            pass
        return ""

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

    def human_size(num_bytes: int) -> str:
        # Always work in float for division, but only after checking thresholds.
        if num_bytes < 1024:
            return f"{num_bytes} bytes"
        kb = num_bytes / 1024.0
        if kb < 1024:
            return f"{kb:.1f} KB"
        mb = kb / 1024.0
        if mb < 1024:
            return f"{mb:.1f} MB"
        gb = mb / 1024.0
        if gb < 1024:
            return f"{gb:.2f} GB"
        tb = gb / 1024.0
        return f"{tb:.2f} TB"


    def list_uploaded_files(upload_folder: str):
        items = []
        root = Path(upload_folder)
        if not root.exists():
            return items

        for p in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file():
                continue
            if p.suffix.lower() != ".mp3":
                continue

            st = p.stat()
            ta = safe_get_title_artist(p)
            thumb_url = safe_get_front_cover_data_url(p)

            items.append({
                "name": p.name,
                "size_human": human_size(st.st_size),
                "mtime_human": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "title": ta["title"],
                "artist": ta["artist"],
                "thumb": thumb_url,  # '' if no cover
            })

        return items
    
    def get_sorted_mp3s(upload_folder: str):
        """
        Return a list of Path objects for .mp3 files in upload_folder,
        sorted newest-first (same logic we used in list_uploaded_files()).
        """
        root = Path(upload_folder)
        if not root.exists():
            return []
        files = []
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() == ".mp3":
                files.append(p)
        # sort by mtime desc (newest first)
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return files


    def get_neighbors(upload_folder: str, current_name: str):
        """
        Given the current filename, find the previous and next filenames
        in the sorted list. Returns (prev_name, next_name) or (None, None).
        'prev' means the one that appears just before current in the sort order
        (i.e. more recent), 'next' means just after.
        """
        files = get_sorted_mp3s(upload_folder)
        names = [p.name for p in files]

        if current_name not in names:
            return (None, None)

        idx = names.index(current_name)

        prev_name = names[idx - 1] if idx - 1 >= 0 else None
        next_name = names[idx + 1] if idx + 1 < len(names) else None

        return (prev_name, next_name)


    # --------------- Routes ---------------

    @app.route("/", methods=["GET", "POST"])
    @require_auth
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
            stem = Path(fname).stem
            ext = Path(fname).suffix.lower()
            unique = datetime.now().strftime("%Y%m%d-%H%M%S")
            fname = f"{stem}-{unique}{ext}"
            dest = Path(app.config["UPLOAD_FOLDER"]) / fname
            f.save(dest)
            flash("File uploaded.", "ok")
            return redirect(url_for("edit", filename=fname))
        return render_template("index.html")

    @app.route("/edit/<path:filename>", methods=["GET"])
    @require_auth
    def edit(filename):
        file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
        if not file_path.exists():
            abort(404)

        tags = load_id3(file_path)
        common = get_common(tags)
        cover_front = get_cover_b64(tags, "front")
        cover_back = get_cover_b64(tags, "back")

        prev_name, next_name = get_neighbors(app.config["UPLOAD_FOLDER"], filename)

        return render_template(
            "edit.html",
            filename=filename,
            common=common,
            cover_front=cover_front,
            cover_back=cover_back,
            prev_name=prev_name,
            next_name=next_name,
        )


    @app.post("/update/<path:filename>")
    @require_auth
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
        except MutagenError as e:
            flash(f"ID3 error: {e}", "error")
        else:
            flash("Tags saved.", "ok")
        return redirect(url_for("edit", filename=filename))

    @app.post("/cover/<path:filename>/add")
    @require_auth
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
    @require_auth
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
    @require_auth
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
    
    @app.route("/download/<path:filename>")
    @require_auth
    def download_updated(filename):
        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            filename,
            as_attachment=True
        )
    
    # simple health check
    @app.get("/_health")
    @require_auth
    def health():
        return "ok", 200


    @app.get("/explore")
    @require_auth
    def explore():
        files = list_uploaded_files(app.config["UPLOAD_FOLDER"])
        return render_template("explore.html", files=files)
    
    @app.post("/delete/<path:filename>")
    def delete_file(filename):
        file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
        if not file_path.exists():
            flash("File not found.", "error")
            return redirect(url_for("explore"))

        try:
            file_path.unlink()
            flash(f"Deleted {filename}", "ok")
        except Exception as e:
            flash(f"Could not delete {filename}: {e}", "error")

        return redirect(url_for("explore"))

    @app.post("/delete-bulk")
    @require_auth
    def delete_bulk():
        # "files" will be an array of checkbox values
        filenames = request.form.getlist("files")
        if not filenames:
            flash("No files selected.", "error")
            return redirect(url_for("explore"))

        deleted = []
        failed = []

        for filename in filenames:
            file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
            try:
                if file_path.exists() and file_path.is_file():
                    file_path.unlink()
                    deleted.append(filename)
                else:
                    failed.append(filename)
            except Exception as e:
                failed.append(f"{filename} ({e})")

        if deleted:
            flash(f"Deleted {len(deleted)} file(s).", "ok")
        if failed:
            flash(f"Could not delete: {', '.join(failed)}", "error")

        return redirect(url_for("explore"))
    
    @app.post("/download-bulk")
    @require_auth
    def download_bulk():
        # Get all selected checkboxes named "files"
        filenames = request.form.getlist("files")
        if not filenames:
            flash("No files selected.", "error")
            return redirect(url_for("explore"))

        # We'll build the zip in memory
        mem_zip = io.BytesIO()
        with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for filename in filenames:
                file_path = Path(app.config["UPLOAD_FOLDER"]) / filename
                # only add real, readable files
                if file_path.exists() and file_path.is_file():
                    # arcname = filename inside the zip
                    zf.write(file_path, arcname=filename)

        mem_zip.seek(0)

        # Build a nice zip filename, like export-2025-10-28_22-41.zip
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        zip_name = f"mp3-export-{stamp}.zip"

        # return as attachment
        return send_file(
            mem_zip,
            mimetype="application/zip",
            as_attachment=True,
            download_name=zip_name
        )
    
    @app.context_processor
    def inject_globals():
        return {
            "APP_VERSION": app.config.get("APP_VERSION", "v0.1-dev")
        }

    return app


app = create_app()

if __name__ == "__main__":
    # Local dev: flask run OR python app_main.py
    app.run(debug=True, host="0.0.0.0", port=5000)
