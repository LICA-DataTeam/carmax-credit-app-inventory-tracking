import streamlit as st

st.set_page_config(page_title="Credit App Inventory Tracking", page_icon=":material/warehouse:")

inv_tracking = st.Page("pages/1_Inventory_Tracking.py", title="Inventory Tracking", icon=":material/inventory_2:")
cred_appli_status = st.Page("pages/2_Credit_Application_Status.py", title="CA Status", icon=":material/credit_card_clock:")
unit_inquiries = st.Page("pages/3_Unit_Inquiries.py", title="Unit Inquiries", icon=":material/ads_click:")

pg = st.navigation([inv_tracking, cred_appli_status, unit_inquiries])
pg.run()