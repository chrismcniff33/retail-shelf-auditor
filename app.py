import streamlit as st
from google import genai
from google.genai import types
import pandas as pd
from PIL import Image
import io
import hmac
import re
import json
import zipfile
import time

# ── 1. PAGE CONFIG ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="AI Shelf Intelligence", page_icon="🔍", layout="wide")
st.markdown("""
    <style>
    .main { padding-top: 1.5rem; }
    .stAlert { margin-top: 0.5rem; }
    </style>
""", unsafe_allow_html=True)


# ── 2. PASSWORD GATE ───────────────────────────────────────────────────────────
def check_password():
    def _submitted():
        correct = st.secrets.get("password", "")
        if hmac.compare_digest(st.session_state.get("pw_input", ""), correct):
            st.session_state["authenticated"] = True
        else:
            st.session_state["authenticated"] = False

    if st.session_state.get("authenticated"):
        return True

    st.text_input(
        "Enter company password to access this tool:",
        type="password",
        key="pw_input",
        on_change=_submitted,
    )
    if st.session_state.get("authenticated") is False:
        st.error("😕 Incorrect password — please try again.")
    return False

if not check_password():
    st.stop()


# ── 3. SESSION STATE ───────────────────────────────────────────────────────────
# All state lives here so it survives every Streamlit re-render.
_DEFAULTS = {
    "image_queue":    [],    # [{"name": str, "bytes": bytes}] — loaded once at upload
    "processing":     False, # True while the audit loop is running
    "current_index":  0,     # which image we are on
    "results":        [],    # list of per-image DataFrames, appended as we go
    "failed_files":   [],    # [{"name": str, "error": str}] — with full error detail
    "audit_complete": False, # flipped to True when every image has been attempted
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ── 4. CONSTANTS ───────────────────────────────────────────────────────────────
# Defined once — referenced everywhere.  Edit here and it propagates.
DESIRED_COLS = [
    "Image name", "Country", "City", "Retailer", "Category",
    "Product name", "Brand", "Manufacturer",
    "Pack size (ml/g)", "Quantity", "Price (local currency)", "Promo",
    "Pack type", "Pack material", "Main colour(s)", "Flavour(s)",
    "Ingredients", "Calories (kcal)", "On-pack claims",
    "Shelf position", "Facings", "Confidence level",
]

COL_RENAME = {
    "Product_Name":  "Product name",
    "Pack_Size":     "Pack size (ml/g)",
    "Price":         "Price (local currency)",
    "Pack_Type":     "Pack type",
    "Pack_Material": "Pack material",
    "Pack_Colour":   "Main colour(s)",
    "Flavour":       "Flavour(s)",
    "Calories":      "Calories (kcal)",
    "On_pack_claims":"On-pack claims",
    "Position":      "Shelf position",
    "Confidence":    "Confidence level",
}

# All fields the AI is expected to return (pre-rename names)
AI_EXPECTED_COLS = list(COL_RENAME.keys()) + [
    "Country", "Category", "Brand", "Manufacturer",
    "Quantity", "Promo", "Ingredients", "Facings",
]


# ── 5. SYSTEM PROMPT ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a global retail data expert strictly adhering to Euromonitor category definitions. Analyze this shelf image.
Context: The image filename suggests the retailer and city.

CRITICAL INSTRUCTION: Scan systematically (top-to-bottom, left-to-right) to ensure absolutely ZERO products are missed.

CRITICAL JSON INSTRUCTIONS:
- You MUST output a strictly valid JSON array of objects.
- Do NOT use unescaped double quotes inside your string values.
- Do NOT wrap the response in markdown blocks (like ```json). Just return the raw array.

--- MANUFACTURER DICTIONARY ---
Use this mapping to assign "Manufacturer". If a brand is not listed, use your internal knowledge.
Postobón S.A.: Postobón, Hit, Cristal, Bretaña, Colombiana, Popular, Freskola, Hipinto, Speed Max, Peak, Sr. Toronjo, Agua Oasis.
PepsiCo: Pepsi, 7Up, Mirinda, Mountain Dew, H2Oh!, Gatorade, Aquafina, Teem, Lipton Ice Tea (JV), Lay's, Doritos.
The Coca-Cola Company: Coca-Cola, Sprite, Fanta, Quatro, Brisa, Manantial, Valle, Del Valle, Powerade, Fuze Tea, Eva, Five Alive.
Quala: Vive100%, Suntea, Saviloe, Ego, Light, Bonyurt (Alpina JV).
Bavaria (AB InBev): Pony Malta, Malta Leona, Aguila, Poker, Club Colombia, Costeña, Corona, Stella Artois, Budweiser.
Heineken N.V.: Heineken, Amstel, Sol, Desperados.
Diageo: Smirnoff, Johnnie Walker, Baileys, Guinness, Malta Guinness, Orijin.
Nestlé: Milo, Nescafé, Pure Life, Nestea, Bikkle.
Suntory / Asahi / GSK: Ribena, Lucozade, Aquarius, Calpis.
La Casera Company: La Casera, Bold, Nirvana.
Rite Foods: Bigi, Fearless, Rite.
TGI Group: Chivita, Hollandia.
Aje Group: Big Cola, Cifrut, Sporade, Cielo, Pulp.
--- END DICTIONARY ---

Task: Extract all visible products and return a JSON list of objects with these exact keys. For ANY unknown value, return an empty string ('').

1.  "Product_Name":   Specific name on label.
2.  "Brand":          Brand name.
3.  "Manufacturer":   Refer to the DICTIONARY above.
4.  "Category":       Map to the most GRANULAR Euromonitor category possible.
5.  "Country":        Identify the Country based on the City/Retailer provided.
6.  "Pack_Size":      Convert soft drinks to ml, solid foods to g. Numbers ONLY. If unreadable, estimate. If impossible, write ''.
7.  "Quantity":       Unit count if visible. Else '1'.
8.  "Price":          Tag price. Numbers ONLY. If missing, write ''.
9.  "Promo":          Promo tag description. If none, write ''.
10. "Pack_Type":      MAX 1 WORD. Packaging type only (e.g., Bottle, Can, Carton, Box, Pouch).
11. "Pack_Material":  MAX 1 WORD. Material only (e.g., Plastic, Glass, Metal, Aluminium, Cardboard).
12. "Pack_Colour":    MAX 3 WORDS. Primary colour(s) of packaging (e.g., Red, Blue and Silver).
13. "Flavour":        MAX 3 WORDS (e.g., Cherry Vanilla, Original). If none, write ''.
14. "Ingredients":    DATA ENRICHMENT — pre-trained knowledge only. List ingredients. If unknown, write ''.
15. "Calories":       DATA ENRICHMENT — pre-trained knowledge only. MAX 3 WORDS (e.g., 45 kcal/100ml). If unknown, write ''.
16. "On_pack_claims": Visible health, taste, or sustainability claims (e.g., 'Zero Sugar'). If none, write ''.
17. "Position":       Shelf level: Top, Middle, or Bottom.
18. "Facings":        Integer — count of identical items visible side-by-side.
19. "Confidence":     'High' if text clearly readable, 'Low' if blurry or estimated.
"""


# ── 6. SIDEBAR ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")
    if "GOOGLE_API_KEY" in st.secrets:
        api_key = st.secrets["GOOGLE_API_KEY"]
        st.success("API key loaded ✅")
    else:
        api_key = st.text_input("Gemini API Key", type="password")
        if not api_key:
            st.warning("API key required to start.")

    st.divider()
    st.subheader("💰 Cost & Usage")
    st.info("Approx. $0.60–$1.00 per 1,000 images processed.")
    st.divider()
    st.markdown("""
    ### 📝 Instructions
    1. **Rename files**: `Retailer-City-ShelfID.jpg`
    2. Upload images or a `.zip` folder
    3. Click **Start Audit**
    4. Results update after each image — safe to download at any time
    """)


# ── 7. HELPER FUNCTIONS ────────────────────────────────────────────────────────

def parse_filename(filename: str) -> tuple[str, str]:
    """Extract retailer and city from Retailer-City-ShelfID.jpg naming convention."""
    try:
        parts = filename.rsplit(".", 1)[0].split("-")
        retailer = parts[0].strip() if len(parts) > 0 else "Unknown"
        city     = parts[1].strip() if len(parts) > 1 else "Unknown"
        return retailer, city
    except Exception:
        return "Unknown", "Unknown"


def compress_image(raw_bytes: bytes) -> bytes | None:
    """
    Resize to max 1024×1024 and re-encode at quality 82.
    Reduces image token cost by ~70 % vs 2048px / quality 95
    while retaining all label detail needed for product identification.
    """
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=82)
            return buf.getvalue()
    except Exception:
        return None


def standardize_pack_size(val) -> str:
    s = str(val).strip().lower()
    if s in ("n/a", "nan", "none", "", "null", "unknown"):
        return ""
    # Determine unit multiplier
    if "ml" in s:
        multiplier = 1
    elif "l" in s:          # litres → ml
        multiplier = 1000
    elif "kg" in s:         # kg → g
        multiplier = 1000
    else:                   # already g or unitless
        multiplier = 1
    m = re.search(r"[\d.]+", s.replace(",", "."))
    if m:
        try:
            n = float(m.group()) * multiplier
            return str(int(n)) if float(n).is_integer() else str(round(n, 1))
        except ValueError:
            return ""
    return ""


def standardize_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Parse, outlier-correct, and reformat the Price column."""
    if "Price" not in df.columns:
        return df

    def _parse(val) -> float | None:
        s = str(val).strip()
        if s.upper() in ("N/A", "NAN", "NONE", "", "UNKNOWN"):
            return None
        s = re.sub(r"[^\d.,]", "", s)
        if not s:
            return None
        # Disambiguate comma vs decimal separator
        if "," in s and "." in s:
            s = (s.replace(".", "").replace(",", ".")
                 if s.rfind(",") > s.rfind(".")
                 else s.replace(",", ""))
        elif "," in s:
            s = s.replace(",", ".") if re.search(r",\d{2}$", s) else s.replace(",", "")
        elif "." in s and re.search(r"\.\d{3}$", s) and s.count(".") == 1:
            s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return None

    df["_p"] = df["Price"].apply(_parse)
    valid = df["_p"].dropna()
    if len(valid) >= 3:
        med = valid.median()
        if med > 0:
            def _fix(p):
                if pd.isna(p) or p == 0:
                    return p
                while p < 0.2 * med:
                    p *= 10
                while p > 5 * med:
                    p /= 10
                return round(p, 2)
            df["_p"] = df["_p"].apply(_fix)

    def _fmt(p) -> str:
        if pd.isna(p):
            return ""
        return str(int(p)) if float(p).is_integer() and p > 100 else f"{p:.2f}"

    df["Price"] = df["_p"].apply(_fmt)
    return df.drop(columns=["_p"])


def highlight_low_confidence(row) -> list[str]:
    if str(row.get("Confidence level", "")).lower() == "low":
        return ["background-color: #fff3cd"] * len(row)
    return [""] * len(row)


def build_results_df() -> pd.DataFrame:
    """Combine all result chunks into one clean, column-ordered DataFrame."""
    chunks = st.session_state["results"]
    if not chunks:
        return pd.DataFrame(columns=DESIRED_COLS)
    df = pd.concat(chunks, ignore_index=True)
    for col in DESIRED_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[DESIRED_COLS]


def load_uploaded_files(uploaded_files) -> list[dict]:
    """
    Read all uploaded files (or zip contents) into memory as bytes.
    Storing bytes in session state means we never depend on a temp directory
    that Streamlit might delete between re-renders.
    """
    queue = []
    for f in uploaded_files:
        if f.name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(f) as z:
                    for info in z.infolist():
                        n = info.filename
                        if (info.is_dir()
                                or "__MACOSX" in n
                                or n.split("/")[-1].startswith(".")):
                            continue
                        if n.lower().endswith((".jpg", ".jpeg", ".png")):
                            compressed = compress_image(z.read(n))
                            if compressed:
                                queue.append({
                                    "name":  n.split("/")[-1],
                                    "bytes": compressed,
                                })
                            else:
                                st.warning(f"⚠️ Could not read `{n.split('/')[-1]}` from zip — skipping.")
            except Exception as e:
                st.warning(f"⚠️ Could not read `{f.name}`: {e}")
        elif f.name.lower().endswith((".jpg", ".jpeg", ".png")):
            compressed = compress_image(f.read())
            if compressed:
                queue.append({"name": f.name, "bytes": compressed})
            else:
                st.warning(f"⚠️ Could not read `{f.name}` — skipping.")
    return queue


def call_gemini(client, image_bytes: bytes, retailer: str, city: str) -> list[dict]:
    """
    Send one image to Gemini and return a list of product dicts.
    Raises a descriptive exception on any failure so the caller can log it.
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            SYSTEM_PROMPT + f"\nContext: This store is in {city}, {retailer}.",
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.4,
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    if not response.text:
        raise ValueError("Gemini returned an empty response.")

    raw = response.text.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error — {e}. Raw response (first 300 chars): {raw[:300]}")

    # Handle model occasionally wrapping the array in an object
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break

    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array, got {type(data).__name__}.")
    if len(data) == 0:
        raise ValueError("API returned a valid JSON array but it contained zero product rows.")

    return data


def process_one_image(client, file_info: dict) -> tuple[pd.DataFrame | None, str | None]:
    """
    Process a single image through the full pipeline.
    Returns (DataFrame, None) on success or (None, error_string) on failure.
    Retries up to 3 times with back-off tuned to the error type.
    """
    name = file_info["name"]
    retailer, city = parse_filename(name)

    image_bytes = file_info["bytes"]
    if image_bytes is None:
        return None, "Image bytes missing — file may have failed to load at upload."

    last_error = "Unknown error"
    for attempt in range(3):
        try:
            data = call_gemini(client, image_bytes, retailer, city)

            df = pd.DataFrame(data)
            if df.empty:
                raise ValueError("DataFrame built from API response is empty.")

            # Normalise placeholder strings
            df = df.replace(
                ["N/A", "n/a", "Unknown", "unknown", "None", "none", "null", "NULL"],
                "",
            )

            # Add context columns
            df["Image name"] = name
            df["Retailer"]   = retailer
            df["City"]        = city

            # Ensure all expected AI columns exist
            for col in AI_EXPECTED_COLS:
                if col not in df.columns:
                    df[col] = ""

            # Normalise numeric fields
            if "Pack_Size" in df.columns:
                df["Pack_Size"] = df["Pack_Size"].apply(standardize_pack_size)
            df = standardize_prices(df)

            # Rename to display column names
            df.rename(columns=COL_RENAME, inplace=True)

            return df, None

        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:                         # still have retries left
                err_lower = last_error.lower()
                if "429" in err_lower or "quota" in err_lower:
                    time.sleep(20)                  # rate limit — wait longer
                elif any(x in err_lower for x in ("timeout", "503", "504")):
                    time.sleep(10 * (attempt + 1))  # transient server error
                elif "json" in err_lower or "empty" in err_lower:
                    time.sleep(3)                   # parse issue — quick retry
                else:
                    time.sleep(5)                   # generic back-off

    return None, last_error


# ── 8. MAIN APP ────────────────────────────────────────────────────────────────
st.title("🔍 AI Shelf Intelligence")

# ─── UPLOAD PANEL ─────────────────────────────────────────────────────────────
# Only shown when idle (not mid-run and not finished)
if not st.session_state["processing"] and not st.session_state["audit_complete"]:

    uploaded = st.file_uploader(
        "Upload shelf images or a `.zip` file — files should be named `Retailer-City-ShelfID.jpg`",
        type=["jpg", "jpeg", "png", "zip"],
        accept_multiple_files=True,
    )

    if uploaded:
        queue = load_uploaded_files(uploaded)
        if not queue:
            st.warning("⚠️ No valid images (.jpg / .jpeg / .png) found in the upload.")
        else:
            st.info(f"✅ **{len(queue)} image(s)** ready to process.")
            if not api_key:
                st.warning("⚠️ Please enter your Gemini API key in the sidebar before starting.")
            else:
                if st.button(f"🚀 Start Audit  ({len(queue)} images)", type="primary"):
                    # Store everything needed for the run in session state.
                    # From this point on, no tempfiles, no local paths — just bytes.
                    st.session_state["image_queue"]   = queue
                    st.session_state["results"]       = []
                    st.session_state["failed_files"]  = []
                    st.session_state["current_index"] = 0
                    st.session_state["processing"]    = True
                    st.session_state["audit_complete"]= False
                    st.rerun()


# ─── PROCESSING LOOP (one image per Streamlit render cycle) ───────────────────
#
# Key design principle: instead of a Python for-loop (which Streamlit can
# interrupt at any re-render), we process ONE image each time this block runs,
# save the result to session state, then call st.rerun() to trigger the next.
# Connection drops simply pause the loop — all completed results are safe.
#
if st.session_state["processing"]:

    queue = st.session_state["image_queue"]
    idx   = st.session_state["current_index"]
    total = len(queue)

    # ── Progress UI
    st.write("### ⏱️ Live Processing")
    progress_bar = st.progress(idx / total if total > 0 else 0)
    status_box   = st.empty()

    if idx < total:
        file_info = queue[idx]
        status_box.info(
            f"🔍 Analyzing image **{idx + 1} of {total}**: `{file_info['name']}`"
        )

        # ── API call
        client = genai.Client(api_key=api_key)
        df_chunk, error = process_one_image(client, file_info)

        # Free the image bytes immediately — no longer needed
        st.session_state["image_queue"][idx]["bytes"] = None

        if df_chunk is not None:
            st.session_state["results"].append(df_chunk)
        else:
            st.session_state["failed_files"].append({
                "name":  file_info["name"],
                "error": error,
            })

        # ── Advance index and update progress
        st.session_state["current_index"] += 1
        progress_bar.progress((idx + 1) / total)

        # ── Show live results table (always visible, always downloadable)
        if st.session_state["results"]:
            live_df = build_results_df()
            completed = len(st.session_state["results"])
            failed_so_far = len(st.session_state["failed_files"])
            st.caption(
                f"✅ {completed} processed  |  "
                f"⚠️ {failed_so_far} failed  |  "
                f"📦 {len(live_df)} products extracted so far"
            )
            st.dataframe(
                live_df.style.apply(highlight_low_confidence, axis=1),
                use_container_width=True,
            )
            # Interim download available at all times
            st.download_button(
                "📥 Download current results (CSV)",
                data=live_df.to_csv(index=False).encode("utf-8"),
                file_name="shelf_intelligence_partial.csv",
                mime="text/csv",
                key=f"dl_interim_{idx}",   # unique key per render to avoid Streamlit warnings
            )

        # ── Trigger next image
        st.rerun()

    else:
        # All images attempted — wrap up
        st.session_state["processing"]    = False
        st.session_state["audit_complete"]= True
        st.rerun()


# ─── RESULTS PANEL ────────────────────────────────────────────────────────────
# Shown when complete OR when processing was interrupted but results exist.
has_results = bool(st.session_state["results"])
is_complete = st.session_state["audit_complete"]

if is_complete or (not st.session_state["processing"] and has_results):

    n_ok     = len(st.session_state["results"])
    n_fail   = len(st.session_state["failed_files"])
    final_df = build_results_df()

    if is_complete:
        st.success(
            f"✅ Audit complete — **{n_ok}** image(s) processed successfully"
            + (f", **{n_fail}** failed." if n_fail else ".")
        )
    else:
        st.warning(
            f"⚠️ Processing was interrupted. **{n_ok}** image(s) completed before interruption."
        )

    # ── Failed file details (expandable, with full error messages)
    if n_fail:
        with st.expander(f"⚠️ {n_fail} image(s) could not be processed — click for details"):
            for item in st.session_state["failed_files"]:
                st.error(f"**{item['name']}**\n\n{item['error']}")

    # ── Final results table
    st.write(
        f"### 📊 Results — {len(final_df)} products across {n_ok} image(s)"
    )
    st.dataframe(
        final_df.style.apply(highlight_low_confidence, axis=1),
        use_container_width=True,
    )

    # ── Download + reset controls
    col_dl, col_reset = st.columns([2, 1])
    with col_dl:
        st.download_button(
            "📥 Download Full Report (CSV)",
            data=final_df.to_csv(index=False).encode("utf-8"),
            file_name="shelf_intelligence_output.csv",
            mime="text/csv",
        )
    with col_reset:
        if st.button("🗑️ Clear & Start New Audit"):
            for k, v in _DEFAULTS.items():
                st.session_state[k] = (
                    []    if isinstance(v, list)
                    else False if isinstance(v, bool)
                    else 0
                )
            st.rerun()
