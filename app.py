import streamlit as st
from openai import OpenAI
import pandas as pd
from PIL import Image
import io
import base64
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
        st.error("Incorrect password — please try again.")
    return False

if not check_password():
    st.stop()


# ── 3. SESSION STATE ───────────────────────────────────────────────────────────
_DEFAULTS = {
    "image_queue":    [],
    "processing":     False,
    "current_index":  0,
    "results":        [],
    "failed_files":   [],
    "audit_complete": False,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ── 4. CONSTANTS ───────────────────────────────────────────────────────────────
DESIRED_COLS = [
    "Image name", "Country", "City", "Retailer", "Category",
    "Product name", "Brand", "Manufacturer",
    "Pack size (ml/g)", "Quantity", "Price (local currency)", "Promo",
    "Pack type", "Pack material", "Main colour(s)", "Flavour/Scent",
    "On-pack claims",
    "Shelf position", "Facings", "Confidence level",
]

COL_RENAME = {
    "Product_Name":   "Product name",
    "Pack_Size":      "Pack size (ml/g)",
    "Price":          "Price (local currency)",
    "Pack_Type":      "Pack type",
    "Pack_Material":  "Pack material",
    "Pack_Colour":    "Main colour(s)",
    "Flavour":        "Flavour/Scent",
    "On_pack_claims": "On-pack claims",
    "Position":       "Shelf position",
    "Confidence":     "Confidence level",
}

AI_EXPECTED_COLS = list(COL_RENAME.keys()) + [
    "Country", "Category", "Brand", "Manufacturer",
    "Quantity", "Promo", "Facings",
]


# ── 5. SYSTEM PROMPT ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a global retail data expert strictly adhering to Euromonitor category definitions. Analyze this shelf image.
Context: The image filename suggests the retailer and city.

CRITICAL INSTRUCTION: Scan systematically (top-to-bottom, left-to-right) to ensure absolutely ZERO products are missed.

--- MANUFACTURER DICTIONARY ---
Use this mapping to assign "Manufacturer". If a brand is not listed, use your internal knowledge.
Postobon S.A.: Postobon, Hit, Cristal, Bretana, Colombiana, Popular, Freskola, Hipinto, Speed Max, Peak, Sr. Toronjo, Agua Oasis.
PepsiCo: Pepsi, 7Up, Mirinda, Mountain Dew, H2Oh!, Gatorade, Aquafina, Teem, Lipton Ice Tea (JV), Lays, Doritos.
The Coca-Cola Company: Coca-Cola, Sprite, Fanta, Quatro, Brisa, Manantial, Valle, Del Valle, Powerade, Fuze Tea, Eva, Five Alive.
Quala: Vive100%, Suntea, Saviloe, Ego, Light, Bonyurt (Alpina JV).
Bavaria (AB InBev): Pony Malta, Malta Leona, Aguila, Poker, Club Colombia, Costena, Corona, Stella Artois, Budweiser.
Heineken N.V.: Heineken, Amstel, Sol, Desperados.
Diageo: Smirnoff, Johnnie Walker, Baileys, Guinness, Malta Guinness, Orijin.
Nestle: Milo, Nescafe, Pure Life, Nestea, Bikkle.
Suntory / Asahi / GSK: Ribena, Lucozade, Aquarius, Calpis.
La Casera Company: La Casera, Bold, Nirvana.
Rite Foods: Bigi, Fearless, Rite.
TGI Group: Chivita, Hollandia.
Aje Group: Big Cola, Cifrut, Sporade, Cielo, Pulp.
--- END DICTIONARY ---

Task: Extract all visible products. Return a JSON object with a single key "products" containing an array of product objects.
Each product object must contain ALL of the following keys. Return an empty string for any unknown value.

1.  "Product_Name":   Specific name on label.
2.  "Brand":          Brand name.
3.  "Manufacturer":   Use the DICTIONARY above. If not listed, use your knowledge.
4.  "Category":       Most granular Euromonitor category possible.
5.  "Country":        Country based on City/Retailer context.
6.  "Pack_Size":      ml (liquids) or g (solids). Numbers ONLY. Priority:
                      a) Read from label (front, side, cap, base).
                      b) Infer from identical products visible elsewhere in the image.
                      c) Apply brand knowledge (e.g. Coca-Cola 330ml can, Heineken 500ml bottle).
                      d) Estimate from visual proportion vs nearby known products.
                      Return empty string only if all four methods fail.
7.  "Quantity":       Unit count if visible, else '1'.
8.  "Price":          Numbers ONLY from price tag. Empty string if missing.
9.  "Promo":          Promo tag description. Empty string if none.
10. "Pack_Type":      ONE WORD — packaging type (Bottle, Can, Carton, Box, Pouch, etc.).
11. "Pack_Material":  ONE WORD — material (Plastic, Glass, Metal, Aluminium, Cardboard, etc.).
12. "Pack_Colour":    MAX 3 WORDS — primary colour(s) of packaging.
13. "Flavour":        MAX 3 WORDS — flavour or scent variant. Empty string if none.
14. "On_pack_claims": Visible health, sustainability or taste claims. Empty string if none.
15. "Position":       Shelf level — Top, Middle, or Bottom.
16. "Facings":        Integer — identical items visible side-by-side.
17. "Confidence":     High if text clearly readable. Low if blurry or estimated.
"""


# ── 6. SIDEBAR ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")
    if "OPENAI_API_KEY" in st.secrets:
        api_key = st.secrets["OPENAI_API_KEY"]
        st.success("API key loaded")
    else:
        api_key = st.text_input("OpenAI API Key", type="password")
        if not api_key:
            st.warning("API key required to start.")

    st.divider()
    st.subheader("Cost and Usage")
    st.info("Approx. $1.50-2.00 per 1,000 images (GPT-4o-mini).")
    st.divider()
    st.markdown("""
    ### Instructions
    1. **Rename files**: Retailer-City-ShelfID.jpg
    2. Upload images or a zip folder
    3. Click Start Audit
    4. Results update after each image
    5. Download available at any time
    """)


# ── 7. HELPER FUNCTIONS ────────────────────────────────────────────────────────

def parse_filename(filename: str) -> tuple[str, str]:
    try:
        parts = filename.rsplit(".", 1)[0].split("-")
        retailer = parts[0].strip() if len(parts) > 0 else "Unknown"
        city     = parts[1].strip() if len(parts) > 1 else "Unknown"
        return retailer, city
    except Exception:
        return "Unknown", "Unknown"


def compress_image(raw_bytes: bytes) -> bytes | None:
    """
    Resize to max 800x800, JPEG quality 85.
    At this resolution GPT-4o-mini reads shelf labels reliably
    and responds in 5-12 seconds per image.
    """
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((800, 800))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


def standardize_pack_size(val) -> str:
    s = str(val).strip().lower()
    if s in ("n/a", "nan", "none", "", "null", "unknown"):
        return ""
    if "ml" in s:
        multiplier = 1
    elif "l" in s:
        multiplier = 1000
    elif "kg" in s:
        multiplier = 1000
    else:
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
    if "Price" not in df.columns:
        return df

    def _parse(val) -> float | None:
        s = str(val).strip()
        if s.upper() in ("N/A", "NAN", "NONE", "", "UNKNOWN"):
            return None
        s = re.sub(r"[^\d.,]", "", s)
        if not s:
            return None
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
    chunks = st.session_state["results"]
    if not chunks:
        return pd.DataFrame(columns=DESIRED_COLS)
    df = pd.concat(chunks, ignore_index=True)
    for col in DESIRED_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[DESIRED_COLS]


def load_uploaded_files(uploaded_files) -> list[dict]:
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
                                st.warning(f"Could not read {n.split('/')[-1]} from zip — skipping.")
            except Exception as e:
                st.warning(f"Could not read {f.name}: {e}")
        elif f.name.lower().endswith((".jpg", ".jpeg", ".png")):
            compressed = compress_image(f.read())
            if compressed:
                queue.append({"name": f.name, "bytes": compressed})
            else:
                st.warning(f"Could not read {f.name} — skipping.")
    return queue


def call_openai(client: OpenAI, image_bytes: bytes, retailer: str, city: str) -> list[dict]:
    """
    Send one shelf image to GPT-4o-mini and return a list of product dicts.
    json_object response format guarantees valid JSON on every call.
    temperature=0.1 ensures consistent, deterministic field extraction.
    """
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=16000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT + f"\nContext: This store is in {city}, {retailer}.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url":    f"data:image/jpeg;base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
    )

    # Warn if the model hit the token limit mid-response — products would be missing
    finish_reason = response.choices[0].finish_reason
    if finish_reason == "length":
        raise ValueError(
            "Response was truncated (finish_reason=length). "
            "The shelf may have more products than the model could output in one pass. "
            "Try splitting the image into closer-cropped sections."
        )

    raw = response.choices[0].message.content
    if not raw:
        raise ValueError("OpenAI returned an empty response.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}. Raw response (first 300 chars): {raw[:300]}")

    # Extract products array — handle {"products": [...]} or bare list
    if isinstance(data, list):
        products = data
    else:
        products = data.get("products", [])
        if not products:
            for v in data.values():
                if isinstance(v, list) and len(v) > 0:
                    products = v
                    break

    if not isinstance(products, list) or len(products) == 0:
        raise ValueError("No product rows found in API response.")

    return products


def process_one_image(
    client: OpenAI, file_info: dict
) -> tuple[pd.DataFrame | None, str | None]:
    """
    Full pipeline for one image: API call, parse, normalise, rename.
    Returns (DataFrame, None) on success or (None, error_string) on failure.
    Retries up to 3 times with backoff tuned to the error type.
    """
    name = file_info["name"]
    retailer, city = parse_filename(name)

    image_bytes = file_info["bytes"]
    if image_bytes is None:
        return None, "Image bytes missing — file may have failed to load at upload."

    last_error = "Unknown error"
    for attempt in range(3):
        try:
            products = call_openai(client, image_bytes, retailer, city)

            df = pd.DataFrame(products)
            if df.empty:
                raise ValueError("DataFrame built from API response is empty.")

            df = df.replace(
                ["N/A", "n/a", "Unknown", "unknown", "None", "none", "null", "NULL"],
                "",
            )

            df["Image name"] = name
            df["Retailer"]   = retailer
            df["City"]        = city

            for col in AI_EXPECTED_COLS:
                if col not in df.columns:
                    df[col] = ""

            if "Pack_Size" in df.columns:
                df["Pack_Size"] = df["Pack_Size"].apply(standardize_pack_size)
            df = standardize_prices(df)
            df.rename(columns=COL_RENAME, inplace=True)

            return df, None

        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:
                err_lower = last_error.lower()
                if "rate_limit" in err_lower or "429" in err_lower or "quota" in err_lower:
                    time.sleep(30 * (3 ** attempt))   # 30s then 90s
                elif any(x in err_lower for x in ("timeout", "503", "504", "500")):
                    time.sleep(15 * (attempt + 1))    # 15s then 30s
                elif "json" in err_lower or "empty" in err_lower:
                    time.sleep(3)
                else:
                    time.sleep(5)

    return None, last_error


# ── 8. MAIN APP ────────────────────────────────────────────────────────────────
st.title("AI Shelf Intelligence")

# ─── UPLOAD PANEL ─────────────────────────────────────────────────────────────
if not st.session_state["processing"] and not st.session_state["audit_complete"]:

    uploaded = st.file_uploader(
        "Upload shelf images or a zip file — name files as Retailer-City-ShelfID.jpg",
        type=["jpg", "jpeg", "png", "zip"],
        accept_multiple_files=True,
    )

    if uploaded:
        queue = load_uploaded_files(uploaded)
        if not queue:
            st.warning("No valid images (.jpg / .jpeg / .png) found in the upload.")
        else:
            st.info(f"{len(queue)} image(s) ready to process.")
            if not api_key:
                st.warning("Please enter your OpenAI API key in the sidebar before starting.")
            else:
                if st.button(f"Start Audit ({len(queue)} images)", type="primary"):
                    st.session_state["image_queue"]    = queue
                    st.session_state["results"]        = []
                    st.session_state["failed_files"]   = []
                    st.session_state["current_index"]  = 0
                    st.session_state["processing"]     = True
                    st.session_state["audit_complete"] = False
                    st.rerun()


# ─── PROCESSING LOOP ──────────────────────────────────────────────────────────
if st.session_state["processing"]:

    queue = st.session_state["image_queue"]
    idx   = st.session_state["current_index"]
    total = len(queue)

    st.write("### Live Processing")
    progress_bar = st.progress(idx / total if total > 0 else 0)
    status_box   = st.empty()

    if idx < total:
        file_info = queue[idx]
        status_box.info(
            f"Analyzing image {idx + 1} of {total}: {file_info['name']}"
        )

        client = OpenAI(api_key=api_key, timeout=60.0)
        df_chunk, error = process_one_image(client, file_info)

        # Free image bytes immediately — keeps memory flat across large batches
        st.session_state["image_queue"][idx]["bytes"] = None

        if df_chunk is not None:
            st.session_state["results"].append(df_chunk)
        else:
            st.session_state["failed_files"].append({
                "name":  file_info["name"],
                "error": error,
            })

        st.session_state["current_index"] += 1
        progress_bar.progress((idx + 1) / total)

        if st.session_state["results"]:
            live_df = build_results_df()
            st.caption(
                f"{len(st.session_state['results'])} processed  |  "
                f"{len(st.session_state['failed_files'])} failed  |  "
                f"{len(live_df)} products extracted so far"
            )
            st.dataframe(
                live_df.style.apply(highlight_low_confidence, axis=1),
                use_container_width=True,
            )
            st.download_button(
                "Download current results (CSV)",
                data=live_df.to_csv(index=False).encode("utf-8"),
                file_name="shelf_intelligence_partial.csv",
                mime="text/csv",
                key=f"dl_interim_{idx}",
            )

        st.rerun()

    else:
        st.session_state["processing"]     = False
        st.session_state["audit_complete"] = True
        st.rerun()


# ─── RESULTS PANEL ────────────────────────────────────────────────────────────
has_results = bool(st.session_state["results"])
is_complete = st.session_state["audit_complete"]

if is_complete or (not st.session_state["processing"] and has_results):

    n_ok     = len(st.session_state["results"])
    n_fail   = len(st.session_state["failed_files"])
    final_df = build_results_df()

    if is_complete:
        st.success(
            f"Audit complete — {n_ok} image(s) processed successfully"
            + (f", {n_fail} failed." if n_fail else ".")
        )
    else:
        st.warning(f"Processing interrupted. {n_ok} image(s) completed.")

    if n_fail:
        with st.expander(f"{n_fail} image(s) failed — click for details"):
            for item in st.session_state["failed_files"]:
                st.error(f"**{item['name']}**\n\n{item['error']}")

    st.write(f"### Results — {len(final_df)} products across {n_ok} image(s)")
    st.dataframe(
        final_df.style.apply(highlight_low_confidence, axis=1),
        use_container_width=True,
    )

    col_dl, col_reset = st.columns([2, 1])
    with col_dl:
        st.download_button(
            "Download Full Report (CSV)",
            data=final_df.to_csv(index=False).encode("utf-8"),
            file_name="shelf_intelligence_output.csv",
            mime="text/csv",
        )
    with col_reset:
        if st.button("Clear and Start New Audit"):
            for k, v in _DEFAULTS.items():
                st.session_state[k] = (
                    []    if isinstance(v, list)
                    else False if isinstance(v, bool)
                    else 0
                )
            st.rerun()
