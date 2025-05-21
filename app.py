from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from dotenv  import load_dotenv
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
import os, requests, datetime, re, io, tempfile
from functools import wraps
from datetime import timedelta
from docx import Document
from docx.enum.text import WD_COLOR_INDEX
import fitz  # PyMuPDF

# ─── ENV / AZURE CONFIG ────────────────────────────────────────────────────────
load_dotenv()
SEARCH_SERVICE_NAME             = os.getenv("SEARCH_SERVICE_NAME")
SEARCH_INDEX_NAME               = os.getenv("SEARCH_INDEX_NAME")
API_KEY                         = os.getenv("API_KEY")
API_VERSION                     = os.getenv("API_VERSION")
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME                  = os.getenv("CONTAINER_NAME")
ACCOUNT_KEY                     = os.getenv("ACCOUNT_KEY")

AZURE_ENDPOINT = (
    f"https://{SEARCH_SERVICE_NAME}.search.windows.net/"
    f"indexes/{SEARCH_INDEX_NAME}/docs/search?api-version={API_VERSION}"
)

# ─── FLASK SETUP ───────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))
app.permanent_session_lifetime = timedelta(minutes=30)

blob_service_client = BlobServiceClient.from_connection_string(
    AZURE_STORAGE_CONNECTION_STRING
)
container_client = blob_service_client.get_container_client(CONTAINER_NAME)

@app.before_request
def keep_session(): session.permanent = True

VALID_CREDENTIALS = {
    os.getenv("ADMIN_USERNAME"): os.getenv("ADMIN_PASSWORD"),
    os.getenv("USER1_USERNAME") : os.getenv("USER1_PASSWORD"),
    os.getenv("USER2_USERNAME") : os.getenv("USER2_PASSWORD"),
}

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def login_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if "username" not in session:
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return inner

def palette(i):
    colors = ["#FFFF00","#40E0D0","#FFC0CB","#90EE90",
              "#98FB98","#ADD8E6","#FFB6C1","#EE82EE"]
    return colors[i % len(colors)]

def highlight_text(text, query):
    if not text: return ""
    for i, word in enumerate(query.split()):
        text = re.sub(fr"({re.escape(word)})",
                      rf'<mark style="background:{palette(i)};">\1</mark>',
                      text, flags=re.I)
    return text

def highlight_docx(blob_client, query):
    try:
        doc = Document(io.BytesIO(blob_client.download_blob().readall()))
        wcol = [WD_COLOR_INDEX.YELLOW, WD_COLOR_INDEX.TURQUOISE, WD_COLOR_INDEX.PINK,
                WD_COLOR_INDEX.GREEN, WD_COLOR_INDEX.BRIGHT_GREEN, WD_COLOR_INDEX.BLUE,
                WD_COLOR_INDEX.RED, WD_COLOR_INDEX.VIOLET]
        cmap = {w.lower(): wcol[i % len(wcol)] for i,w in enumerate(query.split())}
        for p in doc.paragraphs:
            for r in p.runs:
                for w,c in cmap.items():
                    if re.search(re.escape(w), r.text, re.I):
                        r.font.highlight_color = c
        buf = io.BytesIO(); doc.save(buf); buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        app.logger.error(f"DOCX highlight error: {e}")
        return None

def highlight_pdf(blob_client, query):
    try:
        data = blob_client.download_blob().readall()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp.write(data); tmp.close()
        doc = fitz.open(tmp.name)
        pcol = [(1,1,0),(0,0.8,0.8),(1,0.75,0.8),(0.56,0.93,0.56),
                (0.6,0.98,0.6),(0.68,0.85,0.9),(1,0.71,0.76),(0.93,0.51,0.93)]
        cmap = {w.lower(): pcol[i % len(pcol)] for i,w in enumerate(query.split())}
        for pg in doc:
            for w,c in cmap.items():
                for inst in pg.search_for(w, quads=True):
                    annot = pg.add_highlight_annot(inst)
                    annot.set_colors(stroke=c); annot.update()
        buf = io.BytesIO(); doc.save(buf); doc.close(); os.unlink(tmp.name)
        buf.seek(0); return buf.getvalue()
    except Exception as e:
        app.logger.error(f"PDF highlight error: {e}")
        return None

def sas_url(blob_name):
    token = generate_blob_sas(
        account_name   = blob_service_client.account_name,
        container_name = CONTAINER_NAME,
        blob_name      = blob_name,
        account_key    = ACCOUNT_KEY,
        permission     = BlobSasPermissions(read=True),
        expiry         = datetime.datetime.utcnow() + datetime.timedelta(hours=1),
        content_disposition="inline",
    )
    return f"{container_client.url}/{blob_name}?{token}"

# ─── AUTH ROUTES ───────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u, p = request.form.get("username"), request.form.get("password")
        if VALID_CREDENTIALS.get(u) == p:
            session["username"] = u
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))

# ─── UI ROUTES ────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index(): return render_template("index.html")

# Facet helpers ----------------------------------------------------------------
# ─── FACET HELPERS ───────────────────────────────────────────────────────────────
@app.route("/filetypes")
@login_required
def filetypes():
    payload = {"search":"*","facets":["file_type,count:1000"],"top":0}
    hdrs = {"api-key":API_KEY,"Content-Type":"application/json"}
    try:
        res = requests.post(AZURE_ENDPOINT, headers=hdrs, json=payload).json()
        vals = [f["value"] for f in res.get("@search.facets",{}).get("file_type",[])]
        return jsonify({"file_types":vals})
    except Exception as e:
        app.logger.error(f"File-type facet error: {e}")
        return jsonify({"file_types":[]})

@app.route("/uploaders")
@login_required
def uploaders():
    payload = {"search":"*","facets":["uploaded_by,count:1000"],"top":0}
    hdrs = {"api-key":API_KEY,"Content-Type":"application/json"}
    try:
        res = requests.post(AZURE_ENDPOINT, headers=hdrs, json=payload).json()
        vals = [f["value"] for f in res.get("@search.facets",{}).get("uploaded_by",[])]
        return jsonify({"uploaders":vals})
    except Exception as e:
        app.logger.error(f"Uploader facet error: {e}")
        return jsonify({"uploaders":[]})

@app.route("/categories")
@login_required
def categories():
    payload = {"search":"*","facets":["Category,count:1000"],"top":0}
    hdrs = {"api-key":API_KEY,"Content-Type":"application/json"}
    try:
        res = requests.post(AZURE_ENDPOINT, headers=hdrs, json=payload).json()
        vals = [f["value"] for f in res.get("@search.facets",{}).get("Category",[])]
        return jsonify({"categories":vals})
    except Exception as e:
        app.logger.error(f"Category facet error: {e}")
        return jsonify({"categories":[]})

# ─── SEARCH API ────────────────────────────────────────────────────────────────
@app.route("/search", methods=["POST"])
@login_required
def search():
    q         = (request.form.get("query") or "").strip()
    ftype     = request.form.get("file_type","").strip()
    fsize     = request.form.get("size","")
    drange    = request.form.get("date_range","")
    uploader  = request.form.get("uploaded_by","").strip()
    category  = request.form.get("category","").strip()

    # Debug logging
    app.logger.debug(f"Received uploader filter value: {uploader}")
    app.logger.debug(f"Received category filter value: {category}")

    # OData filter
    flist = [f"authorized_users eq '{session['username']}'"]
    if ftype:    flist.append(f"file_type eq '{ftype}'")
    if uploader: 
        # Ensure exact match for uploaded_by field
        flist.append(f"uploaded_by eq '{uploader}'")
    if category:
        # Ensure exact match for Category field
        flist.append(f"Category eq '{category}'")

    if fsize == "small":   flist.append("file_size lt 1048576")
    if fsize == "medium":  flist.append("file_size ge 1048576 and file_size le 10485760")
    if fsize == "large":   flist.append("file_size gt 10485760")

    if drange:
        now = datetime.datetime.utcnow()
        if   drange=="today":     cutoff=now.replace(hour=0,minute=0,second=0,microsecond=0)
        elif drange=="yesterday": cutoff=(now-datetime.timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        elif drange=="last_week": cutoff=now-datetime.timedelta(days=7)
        elif drange=="last_month":cutoff=now-datetime.timedelta(days=30)
        elif drange=="last_year": cutoff=now-datetime.timedelta(days=365)
        else: cutoff=None
        if cutoff: flist.append(f"last_modified ge {cutoff.isoformat()}Z")

    # Debug logging
    app.logger.debug(f"Final filter string: {' and '.join(flist)}")

    payload = {
        "search"      : "*" if not q else q,
        "searchFields": "content,metadata_storage_name",
        "select"      : "content,metadata_storage_name,metadata_storage_path,file_type,file_size,last_modified,uploaded_by,Category",
        "filter"      : " and ".join(flist),
        "top"         : 50,
        "queryType"   : "full",
        "searchMode"  : "all"
    }

    try:
        hdrs={"api-key":API_KEY,"Content-Type":"application/json"}
        # Debug logging
        app.logger.debug(f"Search payload: {payload}")
        response = requests.post(AZURE_ENDPOINT, headers=hdrs, json=payload).json()
        docs = response.get("value", [])
        # Debug logging
        app.logger.debug(f"Number of results: {len(docs)}")

        for d in docs:
            blob=d["metadata_storage_name"]; ext=(blob.split(".")[-1] if "." in blob else "").lower()
            d["file_type"]=ext
            client=container_client.get_blob_client(blob)
            url=sas_url(blob)
            # Highlight if PDF / DOCX
            if ext in ("doc","docx"):
                if (hd:=highlight_docx(client,q)): 
                    temp=f"highlighted_{blob}";container_client.upload_blob(temp,hd,overwrite=True);url=sas_url(temp)
            elif ext=="pdf":
                if (hp:=highlight_pdf(client,q)):
                    temp=f"highlighted_{blob}";container_client.upload_blob(temp,hp,overwrite=True);url=sas_url(temp)
            d["view_url"]=url
            d["highlighted_content"]=highlight_text(d.get("content",""),q)
        return jsonify({"results":docs})
    except Exception as e:
        app.logger.error(f"Search failure: {e}")
        return jsonify({"error":"Search service error"}),500

# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5002, use_reloader=False)
