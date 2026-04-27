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

CRITICAL INSTRUCTION: Before extracting any data, mentally divide the image into sections:
- Left side vs right side (if multiple units or bays are visible)
- Top shelf, upper-middle shelf, lower-middle shelf, bottom shelf within each unit
Work through EVERY section methodically. For each section identify ALL products including those:
- Partially obscured by other products
- Visible through glass or reflections
- At the back of the shelf behind front-row products
- Only partially in frame at the edges
DO NOT stop until every visible product in every section has been captured.
A complete scan of a standard convenience store fridge or shelf typically yields 20-50 product rows.

CRITICAL OUTPUT FORMAT:
- Output each product as a standalone JSON object on its own line (JSONL format).
- Do NOT wrap products in a JSON array. Do NOT add markdown code fences.
- Every line must be a complete, self-contained JSON object.
- Do NOT use unescaped double quotes inside string values.

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

Task: Extract all visible products. Output each product as a standalone JSON object on its own line.
Each object must contain ALL of the following keys. Return an empty string for any unknown value.

1.  "Product_Name":   Specific name on label.
2.  "Brand":          Brand name.
3.  "Manufacturer":   Use the DICTIONARY above. If not listed, use your knowledge.
4.  "Category":       Most granular Euromonitor category possible.
5.  "Country":        Country based on City/Retailer context.
6.  "Pack_Size":      ml (liquids) or g (solids). Numbers ONLY. Priority order:
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
    if "GOOGLE_API_KEY" in st.secrets:
        api_key = st.secrets["GOOGLE_API_KEY"]
        st.success("API key loaded")
    else:
        api_key = st.text_input("Google Gemini API Key", type="password")
        if not api_key:
            st.warning("API key required to start.")

    st.divider()
    st.subheader("Cost and Usage")
    st.info("Approx. $0.60-1.00 per 1,000 images (Gemini 2.0 Flash).")
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
        parts    = filename.rsplit(".", 1)[0].split("-")
        retailer = parts[0].strip() if len(parts) > 0 and parts[0].strip() else "Unknown"
        city     = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Unknown"
        return retailer, city
    except Exception:
        return "Unknown", "Unknown"


def compress_image(raw_bytes: bytes) -> bytes | None:
    """
    Resize to max 1024x1024, JPEG quality 85.
    1024px gives Gemini enough detail to read labels on densely stocked shelves.
    """
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


def split_image_vertical(image_bytes: bytes) -> list[tuple[bytes, str]]:
    """
    Split a tall shelf image into top and bottom halves so each half gets
    full model attention. A 10% overlap between halves ensures products
    at the boundary are not missed.

    Only splits portrait/tall images (height > width). Wide/landscape images
    are returned as-is — they are typically single-shelf rows that don't need
    splitting.

    Returns a list of (section_bytes, section_label) tuples.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            if h <= w:
                # Wide image — no benefit from vertical split
                return [(image_bytes, "full shelf")]

            overlap = int(h * 0.10)
            top    = img.crop((0, 0,                    w, h // 2 + overlap))
            bottom = img.crop((0, h // 2 - overlap,     w, h))

            sections = []
            for section_img, label in [(top, "top half of the shelf"), (bottom, "bottom half of the shelf")]:
                section_img.thumbnail((1024, 1024))
                buf = io.BytesIO()
                section_img.save(buf, format="JPEG", quality=85)
                sections.append((buf.getvalue(), label))
            return sections
    except Exception:
        return [(image_bytes, "full shelf")]


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
    """Read and compress all uploads into memory. Raw bytes are never stored."""
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
                                st.warning(f"Could not read {n.split('/')[-1]} — skipping.")
            except Exception as e:
                st.warning(f"Could not read {f.name}: {e}")
        elif f.name.lower().endswith((".jpg", ".jpeg", ".png")):
            compressed = compress_image(f.read())
            if compressed:
                queue.append({"name": f.name, "bytes": compressed})
            else:
                st.warning(f"Could not read {f.name} — skipping.")
    return queue


def _render_stream_preview(placeholder, raw_products: list[dict]) -> None:
    """
    Render a live preview table from raw (pre-normalisation) product dicts.
    Errors here are swallowed — the preview must never interrupt main processing.
    """
    try:
        df = pd.DataFrame(raw_products)
        df = df.rename(columns={k: v for k, v in COL_RENAME.items() if k in df.columns})
        preview_cols = [c for c in DESIRED_COLS if c in df.columns]
        placeholder.dataframe(
            df[preview_cols] if preview_cols else df,
            use_container_width=True,
        )
    except Exception:
        pass


def _call_single_model(
    client, model_name: str,
    image_bytes: bytes, retailer: str, city: str, section_label: str,
    live_placeholder=None, count_placeholder=None,
) -> list[dict]:
    """
    Run one specific model on a section with the standard stopclock.
    Used by the Flash yield-threshold sweep to call full Flash directly
    without triggering the Flash-Lite→Flash fallback chain.
    """
    context = (
        f"\nContext: This store is in {city}, {retailer}. "
        f"You are scanning the {section_label}."
    )
    stream = client.models.generate_content_stream(
        model=model_name,
        contents=[
            SYSTEM_PROMPT + context,
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=16000,
        ),
    )
    buffer       = ""
    all_products = []
    start        = time.time()
    TIMEOUT      = 25   # Flash gets a slightly longer window than Lite

    for chunk in stream:
        if time.time() - start > TIMEOUT:
            break
        if not chunk.text:
            continue
        buffer += chunk.text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip().rstrip(",")
            if not line or not line.startswith("{"):
                continue
            try:
                product = json.loads(line)
            except json.JSONDecodeError:
                try:
                    from json_repair import repair_json
                    product = json.loads(repair_json(line))
                except Exception:
                    continue
            if isinstance(product, dict) and product:
                all_products.append(product)
                if count_placeholder:
                    count_placeholder.info(
                        f"🔍 Flash sweep {section_label}... "
                        f"**{len(all_products)} product(s) found**"
                    )

    remainder = buffer.strip().rstrip(",")
    if remainder and remainder.startswith("{"):
        try:
            product = json.loads(remainder)
            if isinstance(product, dict) and product:
                all_products.append(product)
        except Exception:
            pass

    return all_products


def call_gemini_streaming(
    client,
    image_bytes: bytes,
    retailer: str,
    city: str,
    section_label: str = "full shelf",
    live_placeholder=None,
    count_placeholder=None,
) -> list[dict]:
    """
    Stream product rows from Gemini in JSONL format (one JSON object per line).

    section_label  — tells the model which part of the image it's seeing
                     (e.g. "top half of the shelf", "bottom half of the shelf").
    count_placeholder — st.empty() updated after EVERY product (real-time ticker).
    live_placeholder  — st.empty() updated every 5 products (rolling table preview).

    Returns raw list of product dicts. Full normalisation happens downstream.
    """
    context = (
        f"\nContext: This store is in {city}, {retailer}. "
        f"You are scanning the {section_label}."
    )

    # Flash-Lite is the primary model — optimised for low latency, thinking disabled,
    # designed for high-volume extraction tasks. Expected ~8-10s per section.
    # Full Flash is the 503-only fallback — used only when Flash-Lite is unavailable.
    model_sequence = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]

    # Hard stopclock per section: break out of the stream after this many seconds
    # and return whatever products have been extracted so far. Prevents any single
    # section call from blowing the ~20s per image target.
    SECTION_TIMEOUT_SECS = 20
    last_model_error = ""

    for model_name in model_sequence:
        try:
            stream = client.models.generate_content_stream(
                model=model_name,
                contents=[
                    SYSTEM_PROMPT + context,
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=16000,
                ),
            )

            buffer       = ""
            all_products = []
            section_start = time.time()
            timed_out     = False

            for chunk in stream:
                # Stopclock — break out after SECTION_TIMEOUT_SECS and return
                # whatever products have been extracted rather than blocking further.
                if time.time() - section_start > SECTION_TIMEOUT_SECS:
                    timed_out = True
                    break
                if not chunk.text:
                    continue
                buffer += chunk.text

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip().rstrip(",")
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        product = json.loads(line)
                    except json.JSONDecodeError:
                        try:
                            from json_repair import repair_json
                            product = json.loads(repair_json(line))
                        except Exception:
                            continue
                    if isinstance(product, dict) and product:
                        all_products.append(product)
                        if count_placeholder:
                            count_placeholder.info(
                                f"🔍 Scanning {section_label} ({model_name})... "
                                f"**{len(all_products)} product(s) found so far**"
                            )
                        if live_placeholder and len(all_products) % 5 == 0:
                            _render_stream_preview(live_placeholder, all_products)

            # Flush remaining buffer
            remainder = buffer.strip().rstrip(",")
            if remainder and remainder.startswith("{"):
                try:
                    product = json.loads(remainder)
                except json.JSONDecodeError:
                    try:
                        from json_repair import repair_json
                        product = json.loads(repair_json(remainder))
                    except Exception:
                        product = None
                if product and isinstance(product, dict):
                    all_products.append(product)

            if live_placeholder and all_products:
                _render_stream_preview(live_placeholder, all_products)
            if count_placeholder and all_products:
                timeout_note = " (stopclock — partial)" if timed_out else ""
                count_placeholder.info(
                    f"✅ {section_label.capitalize()} complete ({model_name}{timeout_note}) — "
                    f"**{len(all_products)} product(s) found**"
                )

            if not all_products:
                raise ValueError(f"No product rows extracted from {section_label}.")

            return all_products  # Success — return immediately

        except Exception as exc:
            err_str   = str(exc)
            err_lower = err_str.lower()
            last_model_error = err_str
            # Only fall back to the next model on 503 / capacity errors
            if any(x in err_lower for x in ("503", "unavailable", "overloaded", "high demand")):
                if model_name != model_sequence[-1]:
                    time.sleep(5)   # brief pause before trying fallback model
                    continue
            raise   # Non-503 errors propagate immediately

    raise ValueError(f"All models failed for {section_label}. Last error: {last_model_error}")


def process_one_image(
    client, file_info: dict, live_placeholder=None, count_placeholder=None
) -> tuple[pd.DataFrame | None, str | None]:
    """
    Full pipeline for one image: split into sections, stream each section,
    deduplicate, normalise, and rename.

    Splitting the image into top and bottom halves gives each half full model
    attention, recovering products on lower shelves that the model ignores
    when shown the full tall image at once.

    Returns (DataFrame, None) on success or (None, error_string) on failure.
    Retries up to 3 times with backoff on API errors.
    """
    name = file_info["name"]
    retailer, city = parse_filename(name)

    image_bytes = file_info["bytes"]
    if image_bytes is None:
        return None, "Image bytes missing — file may have failed to load at upload."

    last_error = "Unknown error"
    for attempt in range(3):
        try:
            if attempt > 0 and live_placeholder:
                live_placeholder.empty()
            if attempt > 0 and count_placeholder:
                count_placeholder.empty()

            # Split tall images into sections — each section gets full model attention
            sections     = split_image_vertical(image_bytes)
            all_data     = []
            section_warns = []

            # Minimum products expected from a section of a well-stocked shelf.
            # If Flash-Lite returns fewer than this, the section is likely dense
            # or partially obscured — run it again with full Flash to sweep up
            # what Lite missed. We take whichever result is larger.
            MIN_SECTION_YIELD = 8

            for section_bytes, section_label in sections:
                try:
                    lite_products = call_gemini_streaming(
                        client, section_bytes, retailer, city,
                        section_label, live_placeholder, count_placeholder,
                    )

                    if len(lite_products) < MIN_SECTION_YIELD:
                        # Lite yield is low — sweep the same section with full Flash
                        if count_placeholder:
                            count_placeholder.info(
                                f"🔄 {section_label.capitalize()} — "
                                f"only {len(lite_products)} products found, "
                                f"running full Flash sweep..."
                            )
                        try:
                            # Temporarily override model sequence for full-Flash pass
                            flash_products = _call_single_model(
                                client, "gemini-2.5-flash",
                                section_bytes, retailer, city, section_label,
                                live_placeholder, count_placeholder,
                            )
                            # Take whichever result is more complete
                            section_products = (
                                flash_products
                                if len(flash_products) > len(lite_products)
                                else lite_products
                            )
                        except Exception:
                            section_products = lite_products  # Flash failed — keep Lite
                    else:
                        section_products = lite_products

                    all_data.extend(section_products)

                except Exception as sec_exc:
                    section_warns.append(f"{section_label}: {sec_exc}")

            if not all_data:
                # Every section failed — surface the errors
                raise ValueError(
                    "No products found in any image section. Section errors: "
                    + " | ".join(section_warns)
                )

            # Deduplicate products that appear in the overlap between sections.
            # Key: brand + product name (case-insensitive). Keep first occurrence.
            seen: set[str] = set()
            unique_data: list[dict] = []
            for product in all_data:
                key = (
                    str(product.get("Brand", "")).lower().strip() + "|" +
                    str(product.get("Product_Name", "")).lower().strip()
                )
                if key not in seen:
                    seen.add(key)
                    unique_data.append(product)

            # Remove malformed rows — either both Brand and Product_Name empty,
            # or the row has a name/brand but is missing more than 10 of the 17
            # expected fields (indicates a parsing artefact, not a real product).
            def _is_valid_product(p: dict) -> bool:
                has_name  = bool(str(p.get("Product_Name", "")).strip())
                has_brand = bool(str(p.get("Brand", "")).strip())
                if not has_name and not has_brand:
                    return False
                # Count populated fields — a real product row should have most fields
                populated = sum(
                    1 for k in [
                        "Product_Name", "Brand", "Manufacturer", "Category",
                        "Country", "Pack_Type", "Pack_Material", "Position", "Confidence"
                    ]
                    if str(p.get(k, "")).strip()
                )
                return populated >= 5

            unique_data = [p for p in unique_data if _is_valid_product(p)]

            df = pd.DataFrame(unique_data)
            if df.empty:
                raise ValueError("DataFrame built from API response is empty.")

            df = df.replace(
                ["N/A", "n/a", "Unknown", "unknown", "None", "none", "null", "NULL"],
                "",
            )
            df["Image name"] = name
            df["Retailer"]   = retailer if retailer else "Unknown"
            df["City"]       = city     if city     else "Unknown"

            for col in AI_EXPECTED_COLS:
                if col not in df.columns:
                    df[col] = ""

            if "Pack_Size" in df.columns:
                df["Pack_Size"] = df["Pack_Size"].apply(standardize_pack_size)
            df = standardize_prices(df)
            df.rename(columns=COL_RENAME, inplace=True)

            # If any sections failed (e.g. 503), warn — but still return what we got
            if section_warns:
                warn_note = "⚠️ Partial results — some sections failed: " + " | ".join(section_warns)
                return df, warn_note   # (df, warning) rather than (None, error)

            return df, None

        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:
                err_lower = last_error.lower()
                if "429" in err_lower or "quota" in err_lower or "resource_exhausted" in err_lower:
                    time.sleep(30 * (3 ** attempt))
                elif any(x in err_lower for x in ("503", "504", "500", "unavailable")):
                    time.sleep(15 * (attempt + 1))
                elif "timeout" in err_lower:
                    time.sleep(10 * (attempt + 1))
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
                st.warning("Please enter your Gemini API key in the sidebar before starting.")
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

        client = genai.Client(api_key=api_key)

        # count_box: real-time ticker updating after every product found
        # stream_table: rolling table preview updating every 5 rows
        count_box    = st.empty()
        stream_table = st.empty()

        df_chunk, error = process_one_image(client, file_info, stream_table, count_box)

        # Clear the streaming UI — cumulative results table takes over below
        count_box.empty()
        stream_table.empty()

        # Free image bytes immediately — keeps memory flat across large batches
        st.session_state["image_queue"][idx]["bytes"] = None

        if df_chunk is not None:
            st.session_state["results"].append(df_chunk)
            if error:   # error here is actually a warning about partial results
                st.session_state["failed_files"].append({
                    "name":  file_info["name"],
                    "error": error,
                })
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
