import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
import time
import io
import hmac
import os
import json
from datetime import datetime

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

# --- 3. LOCAL BUDGET TRACKER (Hidden Failsafe) ---
BUDGET_FILE = "budget_tracker.json"
MONTHLY_BUDGET = 1.00
COST_PER_IMAGE = 0.00035
MAX_IMAGES = int(MONTHLY_BUDGET / COST_PER_IMAGE)

def load_budget_usage():
    """Loads the estimated spend for the current month."""
    current_month = datetime.now().strftime("%Y-%m")
    if os.path.exists(BUDGET_FILE):
        try:
            with open(BUDGET_FILE, "r") as f:
                data = json.load(f)
                if data.get("month") == current_month:
                    return data.get("images_processed", 0)
        except:
            pass
    return 0

def save_budget_usage(image_count):
    """Saves the updated image count to calculate spend."""
    current_month = datetime.now().strftime("%Y-%m")
    with open(BUDGET_FILE, "w") as f:
        json.dump({"month": current_month, "images_processed": image_count}, f)

processed_images = load_budget_usage()

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
    
    # --- CLEAN BUDGET INFO ---
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

# --- 5. SYSTEM PROMPT ---
SYSTEM_PROMPT = """
You are a global retail data expert. Analyze this shelf image. 
Context: The image filename suggests the retailer and city.

Task: Extract all visible products and ENRICH the data with your internal knowledge.
Return a JSON list of objects with these exact keys:

1. "Product_Name": Specific name on label.
2. "Brand": Brand name.
3. "Manufacturer": (Enrichment) Who owns this brand?
4. "Category": (Enrichment) Map to the most GRANULAR Euromonitor category possible. Use 'Black Tea', 'Shampoo', etc.
5. "Country": (Enrichment) Identify the Country based on the City/Retailer provided.
6. "Pack_Size": Weight/Volume if visible. Else 'N/A'.
7. "Quantity": Unit count if visible. Else '1'.
8. "Price": Price on tag. If missing, write 'N/A'.
9. "Promo": Description of any promo tag. If none, write ''.
10. "Position": Shelf level (Top/Middle/Bottom).
11. "Facings": Integer count of identical items side-by-side.
12. "Confidence": 'High' if text is clear, 'Low' if blurry or obstructed.
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
    val = row.get('Confidence', '')
    if isinstance(val, str) and val.lower() == 'low':
        return ['background-color: #fff3cd'] * len(row)
    return [''] * len(row)

# --- 7. EFFICIENCY STEP 1: HIGH-SPEED IMAGE PROCESSOR ---
def prepare_image(uploaded_file):
    """ALWAYS resizes to 1600px. Maximizes speed and minimizes token cost."""
    try:
        image = Image.open(uploaded_file)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        image.thumbnail((1600, 1600))
            
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=85)
        return img_byte_arr.getvalue()
    except Exception as e:
        return None

# --- 8. MAIN APP LOGIC ---
st.title("🔍 AI Shelf Intelligence")

# LOCAL HARD STOP (Checks if local memory remembers hitting the limit)
if processed_images >= MAX_IMAGES:
    st.error("🛑 **Monthly API Spend Limit Reached!** You have hit the $1.00 budget cap for this month. The app will resume on the 1st of next month.")
    st.stop()

uploaded_files = st.file_uploader("Upload Shelf Images", accept_multiple_files=True)

if uploaded_files and api_key:
    if st.button(f"Start Audit ({len(uploaded_files)} Images)"):
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        all_products = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_files = len(uploaded_files)
        
        for i, file in enumerate(uploaded_files):
            status_text.write(f"Analyzing {i+1}/{total_files}: **{file.name}**")
            
            try:
                retailer, city = parse_filename(file.name)
                image_bytes = prepare_image(file)
                
                if image_bytes:
                    # EFFICIENCY STEP 2: NATIVE JSON MODE
                    response = model.generate_content(
                        [SYSTEM_PROMPT + f"\nContext: This store is in {city}, {retailer}.",
                         {"mime_type": "image/jpeg", "data": image_bytes}],
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    if response.text:
                        processed_images += 1
                        save_budget_usage(processed_images)
                        
                        try:
                            df_chunk = pd.read_json(io.StringIO(response.text))
                            
                            if not df_chunk.empty:
                                df_chunk['Image_Name'] = file.name
                                df_chunk['Retailer'] = retailer
                                df_chunk['City'] = city
                                all_products.append(df_chunk)
                            else:
                                st.warning(f"⚠️ No products found in {file.name}.")
                        except ValueError:
                            st.warning(f"⚠️ AI returned invalid data structure for {file.name}.")
                
                time.sleep(1) # Safe delay for paid tier
                
            except Exception as e:
                # GOOGLE CLOUD HARD STOP (Catches the 429 API block from your Google Console quota limit)
                if "429" in str(e) or "Quota" in str(e):
                    st.error("🛑 **API Limit Reached!** Your Google Cloud hard cap has been hit mid-batch. No further images can be processed this month.")
                    st.stop()
                else:
                    st.error(f"❌ Skipped {file.name} due to error: {e}")
            
            progress_bar.progress((i + 1) / total_files)
            
        # --- 9. FINAL TABLE ---
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
            st.error("❌ No data generated. Please check your API Key or Image Quality.")

elif uploaded_files and not api_key:
    st.warning("⚠️ Please enter your API Key in the sidebar or secrets to start.")
