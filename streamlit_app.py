
import io, math, re, requests, pandas as pd, streamlit as st
from datetime import date, datetime, timedelta
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm

st.set_page_config(page_title="NI Monthly Scheduler", page_icon="📅", layout="wide")
st.title("NI Monthly Scheduler")
st.caption("Upload the monthly job sheet → validate → schedule → download the team PDF.")

REQUIRED = ["Job Number", "Site Name", "Post Code", "Due Date"]

def norm(s): return re.sub(r"[^a-z0-9]", "", str(s).lower())

def find_col(df, names):
    m = {norm(c): c for c in df.columns}
    for n in names:
        if norm(n) in m: return m[norm(n)]
    return None

@st.cache_data(show_spinner=False)
def geocode_postcode(pc):
    pc = str(pc).strip().upper()
    r = requests.get("https://api.postcodes.io/postcodes/" + pc.replace(" ","%20"), timeout=10)
    if r.ok and r.json().get("result"):
        x=r.json()["result"]; return float(x["latitude"]), float(x["longitude"])
    return None

def hav(a,b):
    R=6371
    p1,p2=map(math.radians,[a[0],b[0]])
    dp=math.radians(b[0]-a[0]); dl=math.radians(b[1]-a[1])
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

def travel_hours(a,b, road_factor=1.25, avg_mph=45):
    km=hav(a,b)*road_factor
    return (km/1.609344)/avg_mph

def workdays(year, month, half):
    d=date(year,month,1)
    out=[]
    while d.month==month:
        if d.weekday()<5:
            if (half==1 and d.day<=16) or (half==2 and d.day>=17): out.append(d)
        d+=timedelta(days=1)
    return out

def build_schedule(df, teams, men, hours_day, yard_pc):
    jc=find_col(df,["Job Number","Job No"])
    sc=find_col(df,["Site Name","Site"])
    pc=find_col(df,["Post Code","Postcode"])
    dc=find_col(df,["Due Date","Due By"])
    hc=find_col(df,["Contract Hours","Actual Hours","Actual Contracted Hours","Hours","Site Contract Hours"])
    missing=[x for x,c in [("Job Number",jc),("Site Name",sc),("Post Code",pc),("Due Date",dc),("Hours",hc)] if not c]
    if missing: raise ValueError("Missing required columns: "+", ".join(missing))
    x=df.copy()
    x["_job"]=x[jc].astype(str)
    x["_site"]=x[sc].astype(str)
    x["_pc"]=x[pc].astype(str).str.upper().str.strip()
    x["_due"]=pd.to_datetime(x[dc], dayfirst=True, errors="coerce")
    x["_hrs"]=pd.to_numeric(x[hc], errors="coerce")
    bad=x[x["_due"].isna()|x["_hrs"].isna()|x["_pc"].isin(["","NAN","NONE"])]
    if len(bad): raise ValueError(f"{len(bad)} job(s) have missing/invalid due date, postcode or hours. Fix these before scheduling.")
    year=int(x["_due"].dt.year.mode().iloc[0]); month=int(x["_due"].dt.month.mode().iloc[0])
    x["_half"]=(x["_due"].dt.day>16).astype(int)+1

    yard=geocode_postcode(yard_pc)
    if not yard: raise ValueError("Could not locate the yard postcode.")
    coords={}
    prog=st.progress(0, text="Locating postcodes…")
    for i,p in enumerate(x["_pc"].unique()):
        coords[p]=geocode_postcode(p)
        prog.progress((i+1)/len(x["_pc"].unique()), text=f"Locating postcodes… {i+1}/{len(x['_pc'].unique())}")
    prog.empty()
    failed=[p for p,v in coords.items() if not v]
    if failed: raise ValueError("Could not locate postcode(s): "+", ".join(failed[:12]))
    x["_coord"]=x["_pc"].map(coords)
    x["_yard_dist"]=x["_coord"].map(lambda c:hav(yard,c))

    scheduled=[]
    for half in [1,2]:
        pool=x[x["_half"]==half].sort_values("_yard_dist",ascending=False).copy()
        days=workdays(year,month,half)
        day_i=0
        while len(pool):
            if day_i>=len(days): raise ValueError(f"Not enough working days to fit all {('first' if half==1 else 'second')}-half jobs.")
            day=days[day_i]
            # allocate one route per team; seed each with a far job, then greedily choose jobs that move toward yard
            used=set()
            for t in range(1,teams+1):
                avail=pool[~pool.index.isin(used)]
                if avail.empty: break
                seed=avail.sort_values("_yard_dist",ascending=False).iloc[0]
                route=[seed.name]; elapsed=travel_hours(yard,seed["_coord"])+seed["_hrs"]/men
                current=seed["_coord"]; current_dist=seed["_yard_dist"]
                while True:
                    cand=pool[(~pool.index.isin(used|set(route))) & (pool["_yard_dist"]<=current_dist+8)]
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
                    idx,r=picked; route.append(idx); elapsed+=r["_leg"]+r["_hrs"]/men
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
            pool=pool.drop(index=list(used))
            day_i+=1
    return pd.DataFrame(scheduled).sort_values(["Planned Date","Team","Route Order"])

def make_pdf(plan, yard):
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
        tp=plan[plan["Team"]==team]
        for d,g in tp.groupby("Planned Date",sort=True):
            story += [Paragraph(pd.Timestamp(d).strftime("%A %d/%m/%Y"),styles["Heading2"])]
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
st.write("Required information: job number, site, postcode, due date and hours. Common column-name variations are accepted.")

if up:
    df=pd.read_excel(up)
    st.success(f"{len(df)} jobs loaded.")
    st.dataframe(df.head(12),use_container_width=True)
    if st.button("Generate monthly plan",type="primary"):
        try:
            plan=build_schedule(df,int(teams),int(men),float(hours),yard)
            st.session_state["plan"]=plan
            st.session_state["yard"]=yard
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
    st.caption("Prototype routing uses UK postcode coordinates plus estimated road travel. For production, replace the travel estimator with a live road-routing matrix.")
