
import io, math, re, requests, pandas as pd, streamlit as st
from datetime import date, timedelta
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm

st.set_page_config(page_title="NI Monthly Scheduler", page_icon="📅", layout="wide")
st.title("NI Monthly Scheduler")

st.caption("Upload the monthly job sheet → validate → schedule → download the team PDF.")

def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

ALIASES = {
    "job": ["Job Number","Job No","Job No.","Job ID","Job"],
    "site": ["Site Name","Site","Location Name"],
    "postcode": ["Post Code","Postcode","Postal Code"],
    "due": ["Due Date","Due Date (Job)","Due By","Job Due Date","Due"],
    "hours": ["Contract Hours","Actual Hours","Actual Contracted Hours","Site Contract Hours",
              "Hours","Contracted Hours","Job Hours","PPM Hours","Labour Hours","Adjusted Hours"],
}

def auto_col(df, key):
    by_norm = {norm(c): c for c in df.columns}
    for alias in ALIASES[key]:
        if norm(alias) in by_norm:
            return by_norm[norm(alias)]
    # looser contains matching for client exports such as "Due Date (Job)"
    tokens = {
        "job": ["jobnumber","jobno"],
        "site": ["sitename"],
        "postcode": ["postcode"],
        "due": ["duedate","dueby"],
        "hours": ["contracthours","contractedhours","actualhours","sitecontracthours","jobhours","ppmhours","labourhours","adjustedhours"],
    }[key]
    for c in df.columns:
        n=norm(c)
        if any(t in n for t in tokens):
            return c
    return None

@st.cache_data(show_spinner=False)
def ors_key():
    try:
        return st.secrets["ORS_API_KEY"]
    except Exception:
        return None

if ors_key():
    st.success("Routing API connected — real road travel is available.")
else:
    st.warning("Routing API key not found. Add ORS_API_KEY in Streamlit Secrets.")

def geocode_postcode(pc):
    """Geocode a UK postcode through the ORS/HeiGIT geocoder."""
    key = ors_key()
    if not key:
        raise ValueError("ORS_API_KEY is missing from Streamlit Secrets.")
    clean = re.sub(r"\s+", "", str(pc).upper().strip())
    if len(clean) > 3:
        clean = clean[:-3] + " " + clean[-3:]
    url = "https://api.heigit.org/openrouteservice/geocode/search"
    params = {
        "api_key": key,
        "text": clean,
        "boundary.country": "GB",
        "size": 1,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        feats = r.json().get("features", [])
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"]
            return (lat, lon)
    except Exception:
        return None
    return None

def ors_matrix(points):
    """Return a driving-time matrix in hours for [(lat, lon), ...]."""
    key = ors_key()
    if not key:
        raise ValueError("ORS_API_KEY is missing from Streamlit Secrets.")
    locations = [[lon, lat] for lat, lon in points]
    url = "https://api.heigit.org/openrouteservice/v2/matrix/driving-car"
    headers = {"Authorization": key, "Content-Type": "application/json"}
    payload = {"locations": locations, "metrics": ["duration"], "units": "m"}
    r = requests.post(url, headers=headers, json=payload, timeout=45)
    if r.status_code in (401, 403):
        raise ValueError("OpenRouteService rejected the API key. Check ORS_API_KEY in Streamlit Secrets.")
    r.raise_for_status()
    durations = r.json().get("durations")
    if not durations:
        raise ValueError("OpenRouteService did not return driving times.")
    return [[None if v is None else v / 3600.0 for v in row] for row in durations]

def hav(a,b):
    R=6371
    p1,p2=map(math.radians,[a[0],b[0]])
    dp=math.radians(b[0]-a[0]); dl=math.radians(b[1]-a[1])
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

def travel_hours(a,b,road_factor=1.25,avg_mph=45):
    return ((hav(a,b)*road_factor)/1.609344)/avg_mph

def build_travel_lookup(yard, job_coords):
    """Build real road travel times between yard and all jobs."""
    labels = ["__YARD__"] + list(job_coords.keys())
    pts = [yard] + [job_coords[p] for p in job_coords]
    mat = ors_matrix(pts)
    lookup = {}
    for i, a in enumerate(labels):
        for j, b in enumerate(labels):
            lookup[(a, b)] = mat[i][j]
    return lookup


def workdays(year,month,half):
    d=date(year,month,1); out=[]
    while d.month==month:
        if d.weekday()<5 and ((half==1 and d.day<=16) or (half==2 and d.day>=17)):
            out.append(d)
        d+=timedelta(days=1)
    return out

def build_schedule(df, cols, teams, men, hours_day, yard_pc):
    jc,sc,pc,dc,hc=[cols[k] for k in ["job","site","postcode","due","hours"]]
    x=df.copy()
    x["_job"]=x[jc].astype(str)
    x["_site"]=x[sc].astype(str)
    x["_pc"]=x[pc].astype(str).str.upper().str.strip()
    x["_due"]=pd.to_datetime(x[dc],dayfirst=True,errors="coerce")
    x["_hrs"]=pd.to_numeric(x[hc],errors="coerce")
    bad=x[x["_due"].isna()|x["_hrs"].isna()|x["_pc"].isin(["","NAN","NONE"])]
    if len(bad):
        raise ValueError(f"{len(bad)} job(s) have missing/invalid due date, postcode or hours. Fix these before scheduling.")
    year=int(x["_due"].dt.year.mode().iloc[0]); month=int(x["_due"].dt.month.mode().iloc[0])
    x["_half"]=(x["_due"].dt.day>16).astype(int)+1
    # Permanent NI yard: ST16 1BQ. Built in so scheduling does not depend on
    # the external postcode service being able to resolve the yard.
    yard_key = re.sub(r"\s+", "", str(yard_pc).upper())
    if yard_key == "ST161BQ":
        yard = (52.8186, -2.1190)
    else:
        yard = geocode_postcode(yard_pc)
    if not yard:
        raise ValueError("Could not locate the yard postcode. Check the postcode or use ST16 1BQ.")
    coords={}
    prog=st.progress(0,text="Locating postcodes…")
    pcs=x["_pc"].unique()
    for i,p in enumerate(pcs):
        coords[p]=geocode_postcode(p)
        prog.progress((i+1)/len(pcs),text=f"Locating postcodes… {i+1}/{len(pcs)}")
    prog.empty()
    failed=[p for p,v in coords.items() if not v]
    if failed: raise ValueError("Could not locate postcode(s): "+", ".join(failed[:12]))
    x["_coord"]=x["_pc"].map(coords)
    x["_yard_dist"]=x["_coord"].map(lambda c:hav(yard,c))
    scheduled=[]
    for half in [1,2]:
        pool=x[x["_half"]==half].sort_values("_yard_dist",ascending=False).copy()
        days=workdays(year,month,half); day_i=0
        while len(pool):
            if day_i>=len(days): raise ValueError("Not enough working days to fit all jobs.")
            day=days[day_i]; used=set()
            for t in range(1,teams+1):
                avail=pool[~pool.index.isin(used)]
                if avail.empty: break
                seed=avail.sort_values("_yard_dist",ascending=False).iloc[0]
                route=[seed.name]
                elapsed=travel_hours(yard,seed["_coord"])+seed["_hrs"]/men
                current=seed["_coord"]; current_dist=seed["_yard_dist"]
                while True:
                    cand=pool[(~pool.index.isin(used|set(route)))&(pool["_yard_dist"]<=current_dist+8)]
                    if cand.empty: break
                    cand=cand.copy()
                    cand["_leg"]=cand["_coord"].map(lambda c:travel_hours(current,c))
                    cand=cand.sort_values(["_leg","_yard_dist"],ascending=[True,False])
                    picked=None
                    for idx,r in cand.iterrows():
                        trial=elapsed+r["_leg"]+r["_hrs"]/men+travel_hours(r["_coord"],yard)
                        if trial<=hours_day:
                            picked=(idx,r); break
                    if not picked: break
                    idx,r=picked; route.append(idx)
                    elapsed+=r["_leg"]+r["_hrs"]/men
                    current=r["_coord"]; current_dist=r["_yard_dist"]
                elapsed+=travel_hours(current,yard)
                for order,idx in enumerate(route,1):
                    r=pool.loc[idx]
                    scheduled.append({
                        "Planned Date":day,"Team":f"Team {t}","Route Order":order,
                        "Job Number":r["_job"],"Site Name":r["_site"],"Post Code":r["_pc"],
                        "Due Date":r["_due"].date(),"Contract Hours":float(r["_hrs"]),
                        "Team Site Hours":round(float(r["_hrs"])/men,2),
                        "Month Half":"1st Half" if half==1 else "2nd Half",
                        "Estimated Route Day Hours":round(elapsed,2)
                    })
                used.update(route)
            pool=pool.drop(index=list(used)); day_i+=1
    return pd.DataFrame(scheduled).sort_values(["Planned Date","Team","Route Order"])

def make_pdf(plan,yard):
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=landscape(A4),rightMargin=10*mm,leftMargin=10*mm,topMargin=9*mm,bottomMargin=9*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle("t",parent=styles["Title"],fontSize=18,alignment=TA_CENTER)
    small=ParagraphStyle("s",parent=styles["Normal"],fontSize=8)
    cell=ParagraphStyle("c",parent=styles["Normal"],fontSize=8,leading=9)
    story=[]
    teams=list(plan["Team"].drop_duplicates())
    for ti,team in enumerate(teams):
        story += [Paragraph(f"{team} - Monthly Job List",title),
                  Paragraph(f"Start/finish yard: {yard} | Follow jobs in the order shown",small),Spacer(1,4*mm)]
        for d,g in plan[plan["Team"]==team].groupby("Planned Date",sort=True):
            story.append(Paragraph(pd.Timestamp(d).strftime("%A %d/%m/%Y"),styles["Heading2"]))
            data=[["Done","Order","Job No.","Site","Postcode","Due By","Contract Hours"]]
            for _,r in g.sort_values("Route Order").iterrows():
                data.append(["☐",str(int(r["Route Order"])),str(r["Job Number"]),Paragraph(str(r["Site Name"]),cell),
                             str(r["Post Code"]),pd.Timestamp(r["Due Date"]).strftime("%d/%m/%Y"),f'{r["Contract Hours"]:.2f}'])
            tb=Table(data,colWidths=[12*mm,13*mm,23*mm,88*mm,27*mm,27*mm,26*mm],repeatRows=1)
            tb.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1F4E78")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),8),
                ("GRID",(0,0),(-1,-1),0.4,colors.HexColor("#AAAAAA")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                ("ALIGN",(0,0),(2,-1),"CENTER"),("ALIGN",(4,1),(-1,-1),"CENTER"),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F4F6F8")]),
                ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
            story += [tb,Spacer(1,4*mm)]
        story += [Paragraph("Notes: ______________________________________________________________________________________________________",small),
                  Spacer(1,3*mm),Paragraph("Operative signature: ____________________________________    Date: ____________________",small)]
        if ti<len(teams)-1: story.append(PageBreak())
    doc.build(story); return buf.getvalue()

def make_excel(plan):
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as w:
        plan.to_excel(w,index=False,sheet_name="Monthly Plan")
    return buf.getvalue()

with st.sidebar:
    st.header("Plan settings")
    teams=st.number_input("Number of teams",1,10,2)
    men=st.number_input("Men per team",1,10,3)
    hours=st.number_input("Hours per day",4.0,12.0,8.0,0.5)
    yard=st.text_input("Yard postcode","ST16 1BQ")
    st.info(f"Capacity: {int(teams)} teams × {int(men)} men × {hours:g} hours")

up=st.file_uploader("Upload monthly Excel sheet",type=["xlsx","xls"])
st.write("Required information: job number, site, postcode, due date and hours. Common client column names are recognised automatically.")

if up:
    df=pd.read_excel(up)
    st.success(f"{len(df)} jobs loaded.")

    # Display copy only: suppress Excel's 1899/1900 placeholder dates.
    preview=df.head(12).copy()
    for c in preview.columns:
        if "planned" in str(c).lower() and "date" in str(c).lower():
            parsed=pd.to_datetime(preview[c],errors="coerce")
            preview[c]=preview[c].where(~parsed.dt.year.isin([1899,1900]),"")
    st.dataframe(preview,use_container_width=True)

    detected={k:auto_col(df,k) for k in ALIASES}
    st.subheader("Column matching")
    st.caption("The scheduler has matched the columns below. Change any dropdown if the client sheet uses a different heading.")
    cols={}
    labels={"job":"Job number","site":"Site name","postcode":"Postcode","due":"Due date","hours":"Contract hours"}
    options=list(df.columns)
    c1,c2,c3=st.columns(3)
    holders=[c1,c2,c3,c1,c2]
    for holder,(key,label) in zip(holders,labels.items()):
        default=detected[key]
        index=options.index(default) if default in options else 0
        cols[key]=holder.selectbox(label,options,index=index,key=f"map_{key}")
    unmatched=[labels[k] for k,v in detected.items() if v is None]
    if unmatched:
        st.warning("Please check the dropdown selection for: "+", ".join(unmatched))

    if st.button("Generate monthly plan",type="primary"):
        try:
            plan=build_schedule(df,cols,int(teams),int(men),float(hours),yard)
            st.session_state["plan"]=plan; st.session_state["yard"]=yard
        except Exception as e:
            st.error(str(e))

if "plan" in st.session_state:
    plan=st.session_state["plan"]
    st.subheader("Generated plan")
    st.dataframe(plan,use_container_width=True)
    c1,c2=st.columns(2)
    c1.download_button("Download team PDF",make_pdf(plan,st.session_state["yard"]),
                       "NI_Team_Job_Lists.pdf","application/pdf",use_container_width=True)
    c2.download_button("Download master Excel",make_excel(plan),
                       "NI_Monthly_Master_Schedule.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",use_container_width=True)
    st.caption("Prototype travel is estimated from postcode geography. Production version should use a road-routing matrix.")
