# NI Monthly Scheduler — Prototype

A browser-based prototype for turning a monthly outstanding-jobs Excel file into:
- a master monthly schedule
- separate ordered team job lists in PDF

## Scheduling rules
- Jobs due on/before the 16th are first-half work.
- Jobs due after the 16th are second-half work.
- Team site duration = contracted man-hours / men in team.
- The full team day is limited by the configured hours per day and includes estimated travel.
- Jobs are allocated geographically.
- Routes seed from the furthest practical work from ST16 1BQ, then select work progressing back toward the yard.
- Invalid/missing postcode, due date or hours stops generation rather than inventing data.

## Run locally
1. Install Python 3.11+.
2. In this folder run:
   pip install -r requirements.txt
3. Then:
   streamlit run streamlit_app.py

## Put it online
Push this folder to a GitHub repository and deploy `streamlit_app.py` with Streamlit Community Cloud.
For company use, use a private repository/app or your organisation's normal hosting.

## Important prototype limitation
The prototype geocodes UK postcodes using postcodes.io and estimates road travel from geographic distance.
Before production use, replace `travel_hours()` with a road-routing matrix/API so daily travel is based on actual roads and journey times.
