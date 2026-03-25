import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
import time
import io
import hmac
import re
import json
import gc
import zipfile
import tempfile
import os

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

# --- 3. SESSION STATE INITIALIZATION ---
if 'audit_results' not in st.session_state:
    st.session_state['audit_results'] = None
if 'failed_files' not in st.session_state:
    st.session_state['failed_files'] = []
if 'live_data_chunks' not in st.session_state:
    st.session_state['live_data_chunks'] = []

# --- 4. SIDEBAR & API SETUP ---
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
    st.info("Approx. $0.60 per 1,000 image files processed.")
    
    st.divider()
    st.write("### 📝 Instructions")
    st.markdown("""
    1. **Rename files** as: `Retailer-City-ShelfID.jpg`
    2. Upload up to **250 images** (or a `.zip` folder).
    3. Click **Start Audit**.
    4. Watch the live progress.
    5. Download the final Excel report.
    """)

# --- 5. SYSTEM PROMPT ---
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

1. "Product_Name": Specific name on label.
2. "Brand": Brand name.
3. "Manufacturer": Refer to the DICTIONARY above. 
4. "Category": Map to the most GRANULAR Euromonitor category possible.
5. "Country": Identify the Country based on the City/Retailer provided.
6. "Pack_Size": Convert soft drinks to ml, solid foods to g. Numbers ONLY. If unreadable, estimate. If impossible, write ''.
7. "Quantity": Unit count if visible. Else '1'.
8. "Price": Tag price. Numbers ONLY. If missing, write ''.
9. "Promo": Promo tag description. If none, write ''.
10. "Pack_Type": MAX 3 WORDS (e.g., Plastic Bottle, Aluminum Can, Cardboard Box).
11. "Pack_Material": MAX 3 WORDS (e.g., Clear Plastic, Colored Glass, Metal).
12. "Pack_Colour": MAX 3 WORDS (e.g., Red and White, Dark Blue).
13. "Flavour": MAX 3 WORDS (e.g., Cherry Vanilla, Original). If none, write ''.
14. "Ingredients": DATA ENRICHMENT TASK. Rely exclusively on pre-trained knowledge. List the ingredients. Do NOT web search. If unknown, write ''.
15. "Calories": DATA ENRICHMENT TASK. Rely exclusively on pre-trained knowledge. MAX 3 WORDS (e.g., 45 kcal/100ml). Do NOT web search. If unknown, write ''.
16. "On_pack_claims": Extract any visible claims regarding health, taste, sustainability on the package (e.g., 'Zero Sugar', '100% Recyclable'). If none, write ''.
17. "Position": Shelf level (Top/Middle/Bottom).
18. "Facings": Integer count of identical items side-by-side.
19. "Confidence": 'High' if text is clearly readable, 'Low' if blurry/estimated.
"""

# --- 6. HELPER FUNCTIONS ---
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
    val = row.get('Confidence level', '')
    if isinstance(val, str) and val.lower() == 'low':
        return ['background-color: #fff3cd'] * len(row)
    return [''] * len(row)

def standardize_pack_size(val):
    s = str(val).strip().lower()
    if s in ['n/a', 'nan', 'none', '', 'null', 'unknown']: 
        return ''
    
    multiplier = 1
    if 'ml' in s:
        pass
    elif 'l' in s and 'ml' not in s:
        multiplier = 1000
    elif 'kg' in s:
        multiplier = 1000
    elif 'g' in s and 'kg' not in s:
        pass
        
    num_match = re.search(r'[\d\.]+', s.replace(',', '.'))
    if num_match:
        try:
            num = float(num_match.group()) * multiplier
            return str(int(num)) if num.is_integer() else str(num)
        except ValueError:
            return ''
    return ''

def standardize_and_fix_prices(df):
    if 'Price' not in df.columns:
        return df
        
    def extract_number(val):
        s = str(val).strip()
        if s.upper() in ['N/A', 'NAN', 'NONE', '', 'UNKNOWN']: 
            return None
        s = re.sub(r'[^\d.,]', '', s)
        if not s: 
            return None
        
        if ',' in s and '.' in s:
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.', '').replace(',', '.')
            else:
                s = s.replace(',', '')
        elif ',' in s:
            if re.search(r',\d{2}$', s):
                s = s.replace(',', '.')
            else:
                s = s.replace(',', '')
        elif '.' in s and re.search(r'\.\d{3}$', s) and not re.search(r'\.\d{3}\.', s):
            s = s.replace('.', '')
                
        try:
            return float(s)
        except ValueError:
            return None

    df['Clean_Price'] = df['Price'].apply(extract_number)
    
    valid_prices = df['Clean_Price'].dropna()
    if len(valid_prices) >= 3: 
        median_price = valid_prices.median()
        if median_price > 0:
            def fix_outlier(p):
                if pd.isna(p) or p == 0: return p
                while p < 0.2 * median_price:
                    p *= 10
                while p > 5 * median_price:
                    p /= 10
                return round(p, 2)
            
            df['Clean_Price'] = df['Clean_Price'].apply(fix_outlier)
    
    def format_price(p):
        if pd.isna(p): 
            return ''
        if p.is_integer() and p > 100:
            return str(int(p))
        else:
            return f"{p:.2f}"
            
    df['Price'] = df['Clean_Price'].apply(format_price)
    df = df.drop(columns=['Clean_Price'])
    return df

# --- 7. DISK-SPOOLING IMAGE PROCESSOR ---
def prepare_image(image_path):
    try:
        with Image.open(image_path) as image:
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            image.thumbnail((2048, 2048))
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='JPEG', quality=95)
            return img_byte_arr.getvalue()
    except Exception as e:
        return None

def extract_to_disk(uploaded_files, temp_dir):
    extracted_files = []
    for file in uploaded_files:
        if file.name.lower().endswith('.zip'):
            with zipfile.ZipFile(file) as z:
                for info in z.infolist():
                    if info.is_dir() or '__MACOSX' in info.filename or info.filename.split('/')[-1].startswith('.'):
                        continue
                    
                    if info.filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                        z.extract(info, temp_dir)
                        extracted_path = os.path.join(temp_dir, info.filename)
                        clean_name = info.filename.split('/')[-1]
                        extracted_files.append({"name": clean_name, "path": extracted_path})
        else:
            path = os.path.join(temp_dir, file.name)
            with open(path, 'wb') as f:
                f.write(file.getbuffer())
            extracted_files.append({"name": file.name, "path": path})
            
    return extracted_files

# --- 8. MAIN APP LOGIC ---
st.title("🔍 AI Shelf Intelligence")

uploaded_files = st.file_uploader("Upload Shelf Images or a .zip file (Max 250 images total)", type=['jpg', 'jpeg', 'png', 'zip'], accept_multiple_files=True)

if uploaded_files:
    with tempfile.TemporaryDirectory() as temp_dir:
        image_files = extract_to_disk(uploaded_files, temp_dir)
        
        if len(image_files) > 250:
            st.error(f"🛑 **Upload Limit Exceeded!** Your upload contains {len(image_files)} images.")
            st.stop()
        elif len(image_files) == 0:
            st.warning("⚠️ No valid images (.jpg, .jpeg, .png) were found in the upload.")
            st.stop()

        if api_key:
            if st.button(f"Start Audit ({len(image_files)} Images)"):
                
                st.session_state['audit_results'] = None
                st.session_state['failed_files'] = []
                st.session_state['live_data_chunks'] = []
                
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-2.0-flash')
                
                st.write("### ⏱️ Live Processing Progress")
                progress_bar = st.progress(0)
                status_text = st.empty()
                live_table_placeholder = st.empty()
                
                total_files = len(image_files)
                failed_files = []
                
                for i, file_info in enumerate(image_files):
                    file_name = file_info["name"]
                    file_path = file_info["path"]
                    
                    max_retries = 3
                    status_text.write(f"Analyzing {i+1}/{total_files}: **{file_name}**")
                    
                    for attempt in range(max_retries):
                        try:
                            retailer, city = parse_filename(file_name)
                            image_bytes = prepare_image(file_path)
                            
                            if image_bytes:
                                response = model.generate_content(
                                    [SYSTEM_PROMPT + f"\nContext: This store is in {city}, {retailer}.",
                                     {"mime_type": "image/jpeg", "data": image_bytes}],
                                    generation_config={
                                        "response_mime_type": "application/json",
                                        "temperature": 0.4,
                                        "max_output_tokens": 8192
                                    },
                                    request_options={"timeout": 600} 
                                )
                                
                                if response.text:
                                    try:
                                        raw_text = response.text.strip()
                                        if raw_text.startswith("```json"):
                                            raw_text = raw_text[7:]
                                        elif raw_text.startswith("```"):
                                            raw_text = raw_text[3:]
                                        if raw_text.endswith("```"):
                                            raw_text = raw_text[:-3]
                                        raw_text = raw_text.strip()
                                        
                                        parsed_json = json.loads(raw_text)
                                        
                                        if isinstance(parsed_json, dict):
                                            for key, value in parsed_json.items():
                                                if isinstance(value, list):
                                                    parsed_json = value
                                                    break
                                        
                                        df_chunk = pd.DataFrame(parsed_json)
                                        
                                        if not df_chunk.empty:
                                            # Filter out any lingering conversational defaults
                                            df_chunk = df_chunk.replace(['N/A', 'n/a', 'Unknown', 'unknown', 'None'], '')

                                            df_chunk['Image name'] = file_name
                                            df_chunk['Retailer'] = retailer
                                            df_chunk['City'] = city
                                            
                                            expected_ai_cols = ["Country", "Category", "Product_Name", "Brand", "Manufacturer", "Pack_Size", "Quantity", "Price", "Promo", "Pack_Type", "Pack_Material", "Pack_Colour", "Flavour", "Ingredients", "Calories", "On_pack_claims", "Position", "Facings", "Confidence"]
                                            for col in expected_ai_cols:
                                                if col not in df_chunk.columns:
                                                    df_chunk[col] = ""

                                            if 'Pack_Size' in df_chunk.columns:
                                                df_chunk['Pack_Size'] = df_chunk['Pack_Size'].apply(standardize_pack_size)
                                            df_chunk = standardize_and_fix_prices(df_chunk)
                                            
                                            df_chunk.rename(columns={
                                                'Product_Name': 'Product name',
                                                'Pack_Size': 'Pack size (ml/g)', 
                                                'Price': 'Price (local currency)',
                                                'Pack_Type': 'Pack type',
                                                'Pack_Material': 'Pack material',
                                                'Pack_Colour': 'Pack colour(s)',
                                                'Flavour': 'Flavour(s)',
                                                'Calories': 'Calories (kcal)',
                                                'On_pack_claims': 'On-pack claims',
                                                'Position': 'Shelf position',
                                                'Confidence': 'Confidence level'
                                            }, inplace=True)
                                            
                                            st.session_state['live_data_chunks'].append(df_chunk)
                                            
                                            interim_df = pd.concat(st.session_state['live_data_chunks'], ignore_index=True)
                                            
                                            desired_order = [
                                                "Image name", "Country", "City", "Retailer", "Category", 
                                                "Product name", "Brand", "Manufacturer", 
                                                "Pack size (ml/g)", "Quantity", "Price (local currency)", "Promo", 
                                                "Pack type", "Pack material", "Pack colour(s)", "Flavour(s)", 
                                                "Ingredients", "Calories (kcal)", "On-pack claims", 
                                                "Shelf position", "Facings", "Confidence level"
                                            ]
                                            for col in desired_order:
                                                if col not in interim_df.columns:
                                                    interim_df[col] = ""
                                            
                                            live_table_placeholder.dataframe(interim_df[desired_order].style.apply(highlight_low_confidence, axis=1))
                                            
                                            del image_bytes
                                            del response
                                            del parsed_json
                                            gc.collect()
                                            
                                            time.sleep(4)
                                            break 
                                            
                                        else:
                                            if attempt == max_retries - 1:
                                                failed_files.append(file_name)
                                            else:
                                                time.sleep(2)
                                                continue
                                                
                                    except Exception as parse_error:
                                        if attempt == max_retries - 1:
                                            failed_files.append(file_name)
                                        else:
                                            time.sleep(2)
                                            continue
                                            
                        except Exception as e:
                            if attempt < max_retries - 1:
                                error_msg = str(e).lower()
                                if "429" in error_msg or "quota" in error_msg:
                                    time.sleep(15) 
                                elif "timeout" in error_msg or "503" in error_msg or "504" in error_msg:
                                    time.sleep(10 * (attempt + 1)) 
                                else:
                                    time.sleep(5)  
                            else:
                                failed_files.append(file_name)
                                break 
                    
                    progress_bar.progress((i + 1) / total_files)
                    
                # --- 9. BATCH FINISHED: FORMAT FINAL MEMORY STATE ---
                status_text.empty() 
                progress_bar.empty() 
                live_table_placeholder.empty() 
                
                if st.session_state['live_data_chunks']:
                    final_df = pd.concat(st.session_state['live_data_chunks'], ignore_index=True)
                    
                    desired_order = [
                        "Image name", "Country", "City", "Retailer", "Category", 
                        "Product name", "Brand", "Manufacturer", 
                        "Pack size (ml/g)", "Quantity", "Price (local currency)", "Promo", 
                        "Pack type", "Pack material", "Pack colour(s)", "Flavour(s)", 
                        "Ingredients", "Calories (kcal)", "On-pack claims", 
                        "Shelf position", "Facings", "Confidence level"
                    ]
                    
                    for col in desired_order:
                        if col not in final_df.columns:
                            final_df[col] = ""
                    
                    final_df = final_df[desired_order]
                    
                    st.session_state['audit_results'] = final_df
                    st.session_state['failed_files'] = failed_files
                    st.session_state['live_data_chunks'] = [] 
                else:
                    st.error("❌ No data generated from this batch.")

elif not api_key:
    st.warning("⚠️ Please enter your API Key in the sidebar or secrets to start.")

# --- 10. DISPLAY PERSISTENT FINAL RESULTS ---
if st.session_state.get('audit_results') is not None:
    st.success("✅ Audit Complete & Data Saved!")
    
    if st.session_state['failed_files']:
        st.warning(f"⚠️ The following images were excluded due to API timeouts or unreadable data: {', '.join(st.session_state['failed_files'])}")
        
    st.write("### 📊 Final Audit Data Preview")
    st.dataframe(st.session_state['audit_results'].style.apply(highlight_low_confidence, axis=1))
    
    csv = st.session_state['audit_results'].to_csv(index=False).encode('utf-8')
    
    col1, col2 = st.columns([1, 4])
    with col1:
        st.download_button(
            label="📥 Download Excel/CSV Report",
            data=csv,
            file_name="ai_shelf_intelligence_data.csv",
            mime="text/csv"
        )
    with col2:
        if st.button("🗑️ Clear Results & Start Fresh"):
            st.session_state['audit_results'] = None
            st.session_state['failed_files'] = []
            st.rerun()

# --- 11. CRASH RECOVERY BLOCK ---
elif len(st.session_state.get('live_data_chunks', [])) > 0:
    st.warning("⚠️ Processing was interrupted (likely due to a browser tab sleeping/disconnecting). Showing partial results recovered from memory.")
    
    interim_df = pd.concat(st.session_state['live_data_chunks'], ignore_index=True)
    
    desired_order = [
        "Image name", "Country", "City", "Retailer", "Category", 
        "Product name", "Brand", "Manufacturer", 
        "Pack size (ml/g)", "Quantity", "Price (local currency)", "Promo", 
        "Pack type", "Pack material", "Pack colour(s)", "Flavour(s)", 
        "Ingredients", "Calories (kcal)", "On-pack claims", 
        "Shelf position", "Facings", "Confidence level"
    ]
    for col in desired_order:
        if col not in interim_df.columns:
            interim_df[col] = ""
            
    interim_df = interim_df[desired_order]
    
    st.dataframe(interim_df.style.apply(highlight_low_confidence, axis=1))
    
    csv = interim_df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="📥 Download Partial Report",
        data=csv,
        file_name="partial_ai_shelf_data.csv",
        mime="text/csv"
    )
