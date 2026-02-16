import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
import time
import io
import hmac
import json
import re

# --- 1. PAGE CONFIGURATION ---
st.set_page_config(page_title="AI Shelf Intelligence", page_icon="🔍", layout="wide")

st.markdown("""
    <style>
    .main { padding-top: 2rem; }
    .stAlert { margin-top: 1rem; }
    </style>
    """, unsafe_allow_html=True)

# --- 2. PASSWORD PROTECTION ---
def check_password():
    def password_entered():
        if hmac.compare_digest(st.session_state["password"], st.secrets["password"]):
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.text_input("Please enter the company password to access this tool:", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.text_input("Please enter the company password to access this tool:", type="password", on_change=password_entered, key="password")
        st.error("😕 Password incorrect")
        return False
    else:
        return True

if not check_password():
    st.stop()

# --- 3. SIDEBAR & API SETUP ---
with st.sidebar:
    st.header("⚙️ Settings")
    if 'GOOGLE_API_KEY' in st.secrets:
        api_key = st.secrets['GOOGLE_API_KEY']
        st.success("API Key loaded from system ✅")
    else:
        api_key = st.text_input("Enter Google Gemini API Key", type="password")
        if not api_key:
            st.warning("Please enter your API Key to proceed.")
    
    st.divider()
    st.subheader("💰 Cost & Usage")
    st.info("""
    **Free Tier:** ~15 images / minute.
    **Paid Tier:** ~$0.35 per 1,000 images.
    """)
    st.divider()
    st.write("### 📝 Instructions")
    st.markdown("""
    1. **Rename files** as: `Retailer-City-ShelfID.jpg`
    2. Upload up to **100 images**.
    3. Click **Start Audit**.
    4. Download the Excel report.
    """)

# --- 4. SYSTEM PROMPT ---
SYSTEM_PROMPT = """
You are a global retail data expert. Analyze this shelf image. 
Context: The image filename suggests the retailer and city.

Task: Extract all visible products and ENRICH the data with your internal knowledge.
Return a strictly valid JSON list of objects with these exact keys:

1. "Product_Name": Specific name on label.
2. "Brand": Brand name.
3. "Manufacturer": (Enrichment) Who owns this brand? (e.g., for Twinings write 'Associated British Foods').
4. "Category": (Enrichment) Map to the most GRANULAR Euromonitor category possible. 
   - Do NOT use high-level aggregations like 'Hot Drinks' or 'Beauty'.
   - USE specific sub-categories: e.g., 'Black Tea', 'Green Tea', 'Instant Coffee', 'Shampoo', 'Conditioner', 'Facial Cleansers'.
5. "Pack_Size": Weight/Volume if visible (e.g., '500g', '1L'). Else 'N/A'.
6. "Quantity": Unit count if visible (e.g., '160 bags'). Else '1'.
7. "Price": Price on tag. If missing, write 'N/A'.
8. "Promo": Description of any yellow/red promo tag (e.g., 'Buy 1 Get 1'). If none, write ''.
9. "Position": Shelf level (Top/Middle/Bottom).
10. "Facings": Integer count of identical items side-by-side.
11. "Confidence": 'High' if text is clear, 'Low' if blurry or obstructed.

Output STRICT JSON only. No markdown.
"""

# --- 5. HELPER FUNCTIONS ---
def parse_filename(filename):
    try:
        name = filename.rsplit('.', 1)[0]
        parts = name.split('-')
        retailer = parts[0] if len(parts) > 0 else "Unknown"
        city = parts[1] if len(parts) > 1 else "Unknown"
        return retailer, city
    except:
        return "Unknown", "Unknown"

def highlight_low_confidence(row):
    val = row.get('Confidence', '')
    if isinstance(val, str) and val.lower() == 'low':
        return ['background-color: #fff3cd'] * len(row)
    return [''] * len(row)

def determine_country(city, retailer):
    city_clean = city.lower().strip()
    retailer_clean = retailer.lower().strip()
    
    city_map = {
        'london': 'UK', 'manchester': 'UK', 'birmingham': 'UK', 'leeds': 'UK', 'glasgow': 'UK',
        'dublin': 'Ireland', 'cork': 'Ireland',
        'paris': 'France', 'lyon': 'France', 'marseille': 'France',
        'berlin': 'Germany', 'munich': 'Germany', 'hamburg': 'Germany', 'frankfurt': 'Germany',
        'new york': 'USA', 'chicago': 'USA', 'los angeles': 'USA', 'miami': 'USA', 'houston': 'USA',
        'toronto': 'Canada', 'vancouver': 'Canada',
        'sydney': 'Australia', 'melbourne': 'Australia',
        'tokyo': 'Japan', 'osaka': 'Japan'
    }
    if city_clean in city_map: return city_map[city_clean]

    retailer_map = {
        'tesco': 'UK', 'sainsburys': 'UK', 'asda': 'UK', 'waitrose': 'UK', 'morrisons': 'UK',
        'dunnes': 'Ireland', 'supervalu': 'Ireland',
        'walmart': 'USA', 'target': 'USA', 'kroger': 'USA', 'whole foods': 'USA',
        'carrefour': 'France', 'leclerc': 'France',
        'edeka': 'Germany', 'rewe': 'Germany', 'aldi': 'Germany', 'lidl': 'Germany',
        'woolworths': 'Australia', 'coles': 'Australia'
    }
    for key, country in retailer_map.items():
        if key in retailer_clean: return country
            
    return "Unknown"

# --- 6. ROBUST IMAGE PROCESSOR ---
def process_image(uploaded_file):
    """Sanitizes image to standard RGB JPEG to prevent format errors."""
    try:
        image = Image.open(uploaded_file)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=95)
        return img_byte_arr.getvalue()
    except Exception as e:
        st.error(f"Could not convert image {uploaded_file.name}. Error: {e}")
        return None

# --- 7. ROBUST JSON PARSER ---
def clean_and_parse_json(text_response):
    """
    Attempts to fix common AI JSON errors (like extra text or missing brackets).
    """
    try:
        # 1. Try direct cleaning
        clean_text = text_response.replace("```json", "").replace("```", "").strip()
        return pd.read_json(io.StringIO(clean_text))
    except ValueError:
        try:
            # 2. Advanced regex extraction: Find the first '[' and last ']'
            match = re.search(r'\[.*\]', text_response, re.DOTALL)
            if match:
                json_str = match.group(0)
                return pd.read_json(io.StringIO(json_str))
        except:
            pass
    return None

# --- 8. MAIN APP LOGIC ---
st.title("🔍 AI Shelf Intelligence")
st.markdown("Use AI to generate structured data tables that provide insight into shelf dynamics across key retailers and channels globally")

uploaded_files = st.file_uploader("Upload Shelf Images", accept_multiple_files=True)

if uploaded_files and api_key:
    if st.button(f"Start Audit ({len(uploaded_files)} Images)"):
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-flash-latest')
        
        all_products = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_files = len(uploaded_files)
        
        for i, file in enumerate(uploaded_files):
            try:
                status_text.write(f"Analyzing {i+1}/{total_files}: **{file.name}**")
                
                # Metadata
                retailer, city = parse_filename(file.name)
                country = determine_country(city, retailer)
                
                # Image Processing
                image_bytes = process_image(file)
                
                if image_bytes:
                    # AI Call
                    response = model.generate_content([
                        SYSTEM_PROMPT + f"\nContext: This store is in {city}, {retailer}.",
                        {"mime_type": "image/jpeg", "data": image_bytes}
                    ])
                    
                    # Parse Response
                    if response.text:
                        df_chunk = clean_and_parse_json(response.text)
                        
                        if df_chunk is not None and not df_chunk.empty:
                            # Add Metadata
                            df_chunk['Image_Name'] = file.name
                            df_chunk['Retailer'] = retailer
                            df_chunk['City'] = city
                            df_chunk['Country'] = country
                            all_products.append(df_chunk)
                        else:
                            st.warning(f"⚠️ Image {file.name} processed, but no products were found or data was unclear.")
                
                time.sleep(2)
                
            except Exception as e:
                st.error(f"❌ Critical Error on {file.name}: {e}")
            
            progress_bar.progress((i + 1) / total_files)
            
        # --- 9. FINAL TABLE & EXPORT ---
        if all_products:
            final_df = pd.concat(all_products, ignore_index=True)
            
            desired_order = [
                "Image_Name", "Country", "City", "Retailer", "Category", 
                "Product_Name", "Brand", "Manufacturer", 
                "Pack_Size", "Quantity", "Price", "Promo", 
                "Position", "Facings", "Confidence"
            ]
            
            for col in desired_order:
                if col not in final_df.columns:
                    final_df[col] = ""
            
            final_df = final_df[desired_order]
            
            st.success("✅ Audit Complete!")
            st.write("### 📊 Audit Data Preview")
            st.dataframe(final_df.style.apply(highlight_low_confidence, axis=1))
            
            csv = final_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Download Excel/CSV Report",
                data=csv,
                file_name="ai_shelf_intelligence_data.csv",
                mime="text/csv"
            )
        else:
            st.error("❌ No data was generated. This usually means the API Key is invalid or the images were too blurry for the AI to read.")

elif uploaded_files and not api_key:
    st.warning("⚠️ Please enter your API Key in the sidebar or secrets to start.")
