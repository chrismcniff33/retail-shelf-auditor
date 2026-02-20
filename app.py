import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
import time
import io
import hmac
import re
import json

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
    st.info("Approx. $0.35 per 1,000 image files processed.")
    
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
You are a global retail data expert strictly adhering to Euromonitor category definitions. Analyze this shelf image. 
Context: The image filename suggests the retailer and city.

CRITICAL INSTRUCTION: This is a highly dense display. Scan the image systematically (top-to-bottom, left-to-right) to ensure absolutely ZERO products are missed. Look carefully in the back rows and on the bottom shelves.

CRITICAL JSON INSTRUCTIONS:
- You MUST output a strictly valid JSON array of objects.
- Do NOT use unescaped double quotes inside your string values (e.g., use 'Buy 1 Get 1' instead of "Buy 1 Get 1"). 
- Do NOT wrap the response in markdown blocks (like ```json). Just return the raw array.

--- MANUFACTURER DICTIONARY (Colombia & Nigeria Focus) ---
Use this mapping to assign "Manufacturer". If a brand is not listed, use your internal knowledge.
Postobón S.A.: Postobón, Hit, Cristal, Bretaña, Colombiana, Popular, Freskola, Hipinto, Speed Max, Peak, Sr. Toronjo, Agua Oasis.
PepsiCo: Pepsi, 7Up, Mirinda, Mountain Dew, H2Oh!, Gatorade, Aquafina, Teem, Lipton Ice Tea (JV).
The Coca-Cola Company: Coca-Cola, Sprite, Fanta, Quatro, Brisa, Manantial, Valle, Del Valle, Powerade, Fuze Tea, Eva, Five Alive, Limca, Gold Spot, Schweppes, Minute Maid, SmartWater.
Quala: Vive100%, Suntea, Saviloe, Ego, Light, Bonyurt (Alpina JV).
Bavaria (AB InBev): Pony Malta, Malta Leona.
Nestlé: Milo, Nescafé, Pure Life, Nestea (JV), Bikkle.
Suntory / Asahi / GSK: Ribena, Lucozade, Aquarius, Calpis.
La Casera Company: La Casera, Bold, Nirvana.
Rite Foods: Bigi, Fearless, Rite.
TGI Group: Chivita, Hollandia.
Aje Group: Big Cola, Cifrut, Sporade, Cielo, Pulp.
Monster Beverage Corp: Monster, Predator.
Red Bull GmbH: Red Bull.
Danone: Bonafont, Evian, Volvic.
Dr Pepper Snapple Group: Dr Pepper, Shasta.
Other Nigeria/LatAm Brands: Viju (Viju Ind.), Cway (Cway Group), Capri-Sun (Wild/NBC), Smoov (Lacasera), Sosa (Rite Foods), Malta Guinness (Diageo), Amstel Malta (Heineken), Fayrouz (Heineken), Orijin Zero (Diageo), Zobo (Various), Alpina (Alpina).
--- END DICTIONARY ---

Task: Extract all visible products and return a JSON list of objects with these exact keys:

1. "Product_Name": Specific name on label.
2. "Brand": Brand name.
3. "Manufacturer": Refer to the DICTIONARY above. If missing, use internal knowledge.
4. "Category": 
   - FOR SOFT DRINKS: You MUST use one of these EXACT strings:
     'Still Natural Mineral Bottled Water', 'Still Spring Bottled Water', 'Still Purified Bottled Water',
     'Carbonated Natural Mineral Bottled Water', 'Carbonated Spring Bottled Water', 'Carbonated Purified Bottled Water',
     'Still Flavoured Bottled Water', 'Sparkling Flavoured Bottled Water', 'Functional Bottled Water',
     'Regular Cola Carbonates', 'Reduced Sugar Cola Carbonates',
     'Regular Lemonade/Lime', 'Reduced Sugar Lemonade/Lime',
     'Regular Orange Carbonates', 'Reduced Sugar Orange Carbonates',
     'Regular Tonic Water/Mixers/Other Bitters', 'Reduced Sugar Tonic Water/Mixers/Other Bitters',
     'Regular Other Non-Cola Carbonates', 'Reduced Sugar Other Non-Cola Carbonates',
     'Liquid Concentrates', 'Powder Concentrates',
     '100% Juice', 'Nectars', 'Juice Drinks (up to 24% Juice)', 'Coconut and Other Plant Waters',
     'Regular Still RTD Tea', 'Reduced Sugar Still RTD Tea', 'Carbonated RTD Tea and Kombucha',
     'RTD Coffee',
     'Regular Energy Drinks', 'Reduced Sugar Energy Drinks',
     'Regular Sports Drinks', 'Reduced Sugar Sports Drinks',
     'Asian Speciality Drinks'.
   - FOR OTHER FMCG: Map to the most GRANULAR Euromonitor category possible.
5. "Country": Identify the Country based on the City/Retailer provided.
6. "Pack_Size": Return strictly as a NUMBER. Convert all soft drinks to milliliters (ml) (e.g., if it's 1.5L, output 1500. If 330ml, output 330). Convert all solid foods to grams (g) (e.g., if 1kg, output 1000). If OCR is unreadable, ESTIMATE the volume in ml/g using visual spatial reasoning. Do not write 'ml' or 'g', just the number.
7. "Quantity": Unit count if visible. Else '1'.
8. "Price": Price on tag. Write numbers only if possible. If missing, write 'N/A'.
9. "Promo": Description of any promo tag. If none, write ''.
10. "Position": Shelf level (Top/Middle/Bottom).
11. "Facings": Integer count of identical items side-by-side.
12. "Confidence": 'High' if text is clearly readable, 'Low' if blurry or if Pack_Size was visually estimated.
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

def standardize_pack_size(val):
    s = str(val).strip().lower()
    if s in ['n/a', 'nan', 'none', '', 'null']: 
        return 'N/A'
    
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
            return 'N/A'
    return 'N/A'

def standardize_and_fix_prices(df):
    if 'Price' not in df.columns:
        return df
        
    def extract_number(val):
        s = str(val).strip()
        if s.upper() in ['N/A', 'NAN', 'NONE', '']: 
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
                if pd.isna(p): return p
                while p > 10 * median_price:
                    p /= 10
                while p < 0.1 * median_price and p > 0:
                    p *= 10
                return p
            
            df['Clean_Price'] = df['Clean_Price'].apply(fix_outlier)
    
    def format_price(p):
        if pd.isna(p): 
            return 'N/A'
        if p.is_integer() and p > 100:
            return str(int(p))
        else:
            return f"{p:.2f}"
            
    df['Price'] = df['Clean_Price'].apply(format_price)
    df = df.drop(columns=['Clean_Price'])
    return df

# --- 6. HIGH-DEFINITION IMAGE PROCESSOR ---
def prepare_image(uploaded_file):
    try:
        image = Image.open(uploaded_file)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        image.thumbnail((2500, 2500))
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=95)
        return img_byte_arr.getvalue()
    except Exception as e:
        return None

# --- 7. MAIN APP LOGIC ---
st.title("🔍 AI Shelf Intelligence")

uploaded_files = st.file_uploader("Upload Shelf Images (Max 100)", accept_multiple_files=True)

if uploaded_files:
    if len(uploaded_files) > 100:
        st.error(f"🛑 **Upload Limit Exceeded!** You uploaded {len(uploaded_files)} images. Please upload a maximum of 100 images at a time.")
        st.stop()

if uploaded_files and api_key:
    if st.button(f"Start Audit ({len(uploaded_files)} Images)"):
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        all_products = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_files = len(uploaded_files)
        failed_files = []
        
        for i, file in enumerate(uploaded_files):
            max_retries = 3
            # Write the status cleanly, without exposing the retry loop to the user
            status_text.write(f"Analyzing {i+1}/{total_files}: **{file.name}**")
            
            for attempt in range(max_retries):
                try:
                    retailer, city = parse_filename(file.name)
                    image_bytes = prepare_image(file)
                    
                    if image_bytes:
                        response = model.generate_content(
                            [SYSTEM_PROMPT + f"\nContext: This store is in {city}, {retailer}.",
                             {"mime_type": "image/jpeg", "data": image_bytes}],
                            generation_config={"response_mime_type": "application/json"}
                        )
                        
                        if response.text:
                            try:
                                # 1. Strip out rogue Markdown formatting
                                raw_text = response.text.strip()
                                if raw_text.startswith("```json"):
                                    raw_text = raw_text[7:]
                                elif raw_text.startswith("```"):
                                    raw_text = raw_text[3:]
                                if raw_text.endswith("```"):
                                    raw_text = raw_text[:-3]
                                raw_text = raw_text.strip()
                                
                                # 2. Safely parse the cleaned string into Python
                                parsed_json = json.loads(raw_text)
                                
                                # 3. Catch nested dictionaries
                                if isinstance(parsed_json, dict):
                                    for key, value in parsed_json.items():
                                        if isinstance(value, list):
                                            parsed_json = value
                                            break
                                
                                # 4. Push to DataFrame
                                df_chunk = pd.DataFrame(parsed_json)
                                
                                if not df_chunk.empty:
                                    df_chunk['Image_Name'] = file.name
                                    df_chunk['Retailer'] = retailer
                                    df_chunk['City'] = city
                                    
                                    if 'Pack_Size' in df_chunk.columns:
                                        df_chunk['Pack_Size'] = df_chunk['Pack_Size'].apply(standardize_pack_size)
                                    df_chunk = standardize_and_fix_prices(df_chunk)
                                    
                                    all_products.append(df_chunk)
                                    
                                    # Natural delay for successful generation before moving to the next file
                                    time.sleep(4)
                                    break # Success! Break out of the retry loop.
                                    
                                else:
                                    if attempt == max_retries - 1:
                                        failed_files.append(file.name)
                                    else:
                                        time.sleep(2)
                                        continue # Empty dataframe returned, try again silently
                                        
                            except Exception as parse_error:
                                if attempt == max_retries - 1:
                                    failed_files.append(file.name)
                                else:
                                    time.sleep(2)
                                    continue # JSON crashed, try again silently
                                    
                except Exception as e:
                    if attempt < max_retries - 1:
                        # Silently pause and retry for network/API errors
                        if "429" in str(e) or "Quota" in str(e):
                            time.sleep(15) # Longer cooldown for speed limits
                        else:
                            time.sleep(5)  # Standard cooldown for 500 errors
                    else:
                        failed_files.append(file.name)
                        break # Max retries hit, skip this file entirely
            
            progress_bar.progress((i + 1) / total_files)
            
        # --- 8. FINAL TABLE ---
        if all_products:
            final_df = pd.concat(all_products, ignore_index=True)
            
            final_df.rename(columns={
                'Pack_Size': 'Pack_Size_(ml/g)', 
                'Price': 'Price (local)'
            }, inplace=True)
            
            desired_order = [
                "Image_Name", "Country", "City", "Retailer", "Category", 
                "Product_Name", "Brand", "Manufacturer", 
                "Pack_Size_(ml/g)", "Quantity", "Price (local)", "Promo", 
                "Position", "Facings", "Confidence"
            ]
            
            for col in desired_order:
                if col not in final_df.columns:
                    final_df[col] = ""
            
            final_df = final_df[desired_order]
            
            st.success("✅ Audit Complete!")
            
            # Show the final failure report if any images didn't make it
            if failed_files:
                st.warning(f"⚠️ The following images were excluded due to API timeouts or unreadable data: {', '.join(failed_files)}")
                
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
            st.error("❌ No data generated from this batch.")

elif uploaded_files and not api_key:
    st.warning("⚠️ Please enter your API Key in the sidebar or secrets to start.")
