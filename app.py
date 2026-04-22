import streamlit as st

st.set_page_config(page_title="Credit App Inventory Tracking", page_icon=":material/warehouse:")

inv_tracking = st.Page("pages/1_Inventory_Tracking.py", title="Inventory Tracking", icon=":material/monitoring:")
cred_appli_status = st.Page("pages/2_Credit_Application_Status.py", title="CA Status", icon=":material/done_all:")

pg = st.navigation([inv_tracking, cred_appli_status])
pg.run()