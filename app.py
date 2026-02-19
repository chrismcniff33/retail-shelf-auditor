import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
import time
import io
import hmac

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
# Note: Removed "Do not use markdown" instruction because Native JSON mode handles it automatically.
SYSTEM_PROMPT = """
You are a global retail data expert. Analyze this shelf image. 
Context: The image filename suggests the retailer and city.

Task: Extract all visible products and ENRICH the data with your internal knowledge.
Return a JSON list of objects with these exact keys:

1. "Product_Name": Specific name on label.
2. "Brand": Brand name.
3. "Manufacturer": (Enrichment) Who owns this brand? (e.g., for Twinings write 'Associated British Foods').
4. "Category": (Enrichment) Map to the most GRANULAR Euromonitor category possible. 
   - Do NOT use high-level aggregations like 'Hot Drinks'. Use 'Black Tea', 'Green Tea', 'Instant Coffee'.
5. "Country": (Enrichment) Identify the Country based on the City/Retailer provided in context OR the language on the packaging.
   - Example: If City is 'Bogota', Country is 'Colombia'.
6. "Pack_Size": Weight/Volume if visible (e.g., '500g', '1L'). Else 'N/A'.
7. "Quantity": Unit count if visible (e.g., '160 bags'). Else '1'.
8. "Price": Price on tag. If missing, write 'N/A'.
9. "Promo": Description of any yellow/red promo tag. If none, write ''.
10. "Position": Shelf level (Top/Middle/Bottom).
11. "Facings": Integer count of identical items side-by-side.
12. "Confidence": 'High' if text is clear, 'Low' if blurry or
