import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
import time
import io
import hmac

# --- 1. PAGE CONFIGURATION ---
st.set_page_config(page_title="AI Shelf Intelligence", page_icon="🛒", layout="wide")

# Custom CSS for a professional look
st.markdown("""
    <style>
    .main { padding-top: 2rem; }
    .stAlert { margin-top: 1rem; }
    </style>
    """, unsafe_allow_html=True)

# --- 2. PASSWORD PROTECTION ---
def check_password():
    """Returns `True` if the user has the correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if hmac.compare_digest(st.session_state["password"], st.secrets["password"]):
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # Don't store the password
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        # First run, show input for password
        st.text_input(
            "Please enter the company password to access this tool:", 
            type="password", 
            on_change=password_entered, 
            key="password"
        )
        return False
    elif not st.session_state["password_correct"]:
        # Password incorrect, show input + error
        st.text_input(
            "Please enter the company password to access this tool:", 
            type="password", 
            on_change=password_entered, 
            key="password"
        )
        st.error("😕 Password incorrect")
        return False
    else:
        # Password correct
        return True

if not check_password():
    st.stop()  # Stop execution if password is not correct

# --- 3. SIDEBAR & API SETUP ---
with st.sidebar:
    st.header("⚙️ Settings")
    
    # Try to load API Key from Secrets, otherwise ask user
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
    
    *Batch processing includes a 2-second delay to prevent rate limit errors.*
    """)
    
    st.divider()
    
    st.write("### 📝 Instructions")
    st.markdown("""
    1. **Rename files** as: `Retailer-City-ShelfID.jpg`
    2. Upload up to **100 images**.
    3. Click **Start Audit**.
    4. Download the Excel report.
    """)

# --- 4. SYSTEM PROMPT (UPDATED FOR GRANULARITY) ---
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

# --- 5. HELPER FUNCTIONS (UPDATED LOGIC) ---
def parse_filename(filename):
    """Extracts Retailer and City from 'Retailer-City-ID.jpg'"""
    try:
        name = filename.rsplit('.', 1)[0]
        parts = name.split('-')
        retailer = parts[0] if len(parts) > 0 else "Unknown"
        city = parts[1] if len(parts) > 1 else "Unknown"
        return retailer, city
    except:
        return "Unknown", "Unknown"

def highlight_low_confidence(row):
    """Highlights row in yellow if confidence is Low"""
    val = row.get('Confidence', '')
    if isinstance(val, str) and val.lower() == 'low':
        return ['background-color: #fff3cd'] * len(row)
    return [''] * len(row)

def determine_country(city, retailer):
    """
    Robust mapping for Country based on City first, then Retailer.
    """
    city_clean = city.lower().strip()
    retailer_clean = retailer.lower().strip()
    
    # 1. City Mapping (Most Accurate)
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
    
    if city_clean in city_map:
        return city_map[city_clean]

    # 2. Retailer Mapping (Fallback)
    retailer_map = {
        'tesco': 'UK', 'sainsburys': 'UK', 'asda': 'UK', 'waitrose': 'UK', 'morrisons': 'UK',
        'dunnes': 'Ireland', 'supervalu': 'Ireland',
        'walmart': 'USA', 'target': 'USA', 'kroger': 'USA', 'whole foods': 'USA',
        'carrefour': 'France', 'leclerc': 'France',
        'edeka': 'Germany', 'rewe': 'Germany', 'aldi': 'Germany', 'lidl': 'Germany',
        'woolworths': 'Australia', 'coles': 'Australia'
    }
    
    # Check if retailer string contains key keywords
    for key, country in retailer_map.items():
        if key in retailer_clean:
            return country
            
    return "Unknown"

# --- 6. MAIN APP LOGIC ---
st.title("AI Shelf Intelligence")
st.markdown("Use AI to generate structured data tables that provide insight into shelf dynamics across key retailers and channels globally.")

uploaded_files = st.file_uploader("Upload Shelf Images", type=['jpg', 'jpeg', 'png', 'webp'], accept_multiple_files=True)

if uploaded_files and api_key:
    if st.button(f"Start Audit ({len(uploaded_files)} Images)"):
        
        genai.configure(api_key=api_key)
        # Using the stable alias to avoid 429 errors on free tier
        model = genai.GenerativeModel('gemini-flash-latest')
        
        all_products = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_files = len(uploaded_files)
        
        for i, file in enumerate(uploaded_files):
            try:
                status_text.write(f"Analyzing {i+1}/{total_files}: **{file.name}**")
                
                # 1. Metadata
                retailer, city = parse_filename(file.name)
                # Pass both city and retailer to the logic function
                country = determine_country(city, retailer)
                
                # 2. Image Data
                image_bytes = file.getvalue()
                
                # 3. AI Call
                response = model.generate_content([
                    SYSTEM_PROMPT + f"\nContext: This store is in {city}, {retailer}.",
                    {"mime_type": "image/jpeg", "data": image_bytes}
                ])
                
                # 4. Parse Response
                if response.text:
                    json_str = response.text.replace("```json", "").replace("```", "").strip()
                    df_chunk = pd.read_json(io.StringIO(json_str))
                    
                    # 5. Add Metadata Columns
                    df_chunk['Image_Name'] = file.name
                    df_chunk['Retailer'] = retailer
                    df_chunk['City'] = city
                    df_chunk['Country'] = country
                    
                    all_products.append(df_chunk)
                
                # Rate Limit Safety (2s delay)
                time.sleep(2)
                
            except Exception as e:
                st.error(f"Error processing {file.name}: {e}")
            
            progress_bar.progress((i + 1) / total_files)
            
        # --- 7. FINAL TABLE ---
        if all_products:
            final_df = pd.concat(all_products, ignore_index=True)
            
            # Reorder Columns (Added Image_Name at the start)
            desired_order = [
                "Image_Name", "Country", "City", "Retailer", "Category", 
                "Product_Name", "Brand", "Manufacturer", 
                "Pack_Size", "Quantity", "Price", "Promo", 
                "Position", "Facings", "Confidence"
            ]
            
            # Ensure columns exist
            for col in desired_order:
                if col not in final_df.columns:
                    final_df[col] = ""
            
            final_df = final_df[desired_order]
            
            st.success("✅ Audit Complete!")
            
            # Display
            st.write("### 📊 Audit Data Preview")
            st.dataframe(final_df.style.apply(highlight_low_confidence, axis=1))
            
            # Download
            csv = final_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Download Excel/CSV Report",
                data=csv,
                file_name="ai_shelf_intelligence_data.csv",
                mime="text/csv"
            )
        else:
            st.warning("No data extracted. Please check image quality.")

elif uploaded_files and not api_key:
    st.warning("⚠️ Please enter your API Key in the sidebar or secrets to start.")
