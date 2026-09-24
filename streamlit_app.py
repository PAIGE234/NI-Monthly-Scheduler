
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
    st.caption("Historic/terminated UK postcodes are supported; if necessary the scheduler falls back to the postcode district rather than stopping the whole plan.")
else:
    st.warning("Routing API key not found. Add ORS_API_KEY in Streamlit Secrets.")

def geocode_postcode(pc):
    """Resolve active OR terminated UK postcodes to coordinates."""
    clean = re.sub(r"\s+", "", str(pc).upper().strip())
    if not clean:
        return None

    # 1) Active postcode lookup.
    try:
        r = requests.get("https://api.postcodes.io/postcodes/" + clean, timeout=20)
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("result"):
            res = data["result"]
            if res.get("latitude") is not None and res.get("longitude") is not None:
                return (float(res["latitude"]), float(res["longitude"]))

        # New Postcodes.io responses can include terminated details in the 404.
        term = data.get("terminated") if isinstance(data, dict) else None
        if term and term.get("latitude") is not None and term.get("longitude") is not None:
            return (float(term["latitude"]), float(term["longitude"]))
    except Exception:
        pass

    # 2) Explicit terminated-postcode lookup.
    try:
        r = requests.get("https://api.postcodes.io/terminated_postcodes/" + clean, timeout=20)
        if r.status_code == 200:
            res = r.json().get("result")
            if res and res.get("latitude") is not None and res.get("longitude") is not None:
                return (float(res["latitude"]), float(res["longitude"]))
    except Exception:
        pass

    # 3) Last-resort outcode centroid, so one historic unit does not stop a whole month.
    # The generated plan can still use ORS road times from this approximate point.
    try:
        spaced = re.sub(r"(\S+)(\d[A-Z]{2})$", r"\1 \2", clean)
        outcode = spaced.split()[0]
        r = requests.get("https://api.postcodes.io/outcodes/" + outcode, timeout=20)
        if r.status_code == 200:
            res = r.json().get("result")
            if res and res.get("latitude") is not None and res.get("longitude") is not None:
                return (float(res["latitude"]), float(res["longitude"]))
    except Exception:
        pass

    return None

def bulk_geocode_postcodes(postcodes):
    """Resolve up to 100 UK postcodes in one Postcodes.io request."""
    cleaned = []
    for pc in postcodes:
        c = re.sub(r"\s+", "", str(pc).upper().strip())
        cleaned.append(c)
    try:
        r = requests.post(
            "https://api.postcodes.io/postcodes?filter=postcode,longitude,latitude",
            json={"postcodes": cleaned},
            timeout=30,
        )
        r.raise_for_status()
        out = {}
        for item in r.json().get("result", []):
            q = re.sub(r"\s+", "", str(item.get("query", "")).upper())
            res = item.get("result")
            if res and res.get("latitude") is not None and res.get("longitude") is not None:
                out[q] = (float(res["latitude"]), float(res["longitude"]))
        return out
    except Exception:
        return {}

def ors_matrix(points):
    """Return real ORS road travel durations (hours) and distances (miles)."""
    key = ors_key()
    if not key:
        raise ValueError("ORS_API_KEY is missing from Streamlit Secrets.")
    locations = [[lon, lat] for lat, lon in points]
    url = "https://api.heigit.org/openrouteservice/v2/matrix/driving-car"
    headers = {"Authorization": key, "Content-Type": "application/json"}
    payload = {"locations": locations, "metrics": ["duration", "distance"]}
    r = requests.post(url, headers=headers, json=payload, timeout=45)
    if r.status_code in (401, 403):
        raise ValueError("OpenRouteService rejected the API key. Check ORS_API_KEY in Streamlit Secrets.")
    r.raise_for_status()
    data = r.json()
    durations = data.get("durations")
    distances = data.get("distances")
    if not durations or not distances:
        raise ValueError("OpenRouteService did not return road travel times and distances.")
    hours = [[None if v is None else v / 3600.0 for v in row] for row in durations]
    miles = [[None if v is None else v / 1609.344 for v in row] for row in distances]
    return hours, miles

def hav(a,b):
    R=6371
    p1,p2=map(math.radians,[a[0],b[0]])
    dp=math.radians(b[0]-a[0]); dl=math.radians(b[1]-a[1])
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

def travel_hours(a,b,road_factor=1.25,avg_mph=45):
    return ((hav(a,b)*road_factor)/1.609344)/avg_mph

def build_travel_lookup(yard, job_coords):
    """Build real road travel time + mileage lookups between yard and all job postcodes."""
    labels = ["__YARD__"] + list(job_coords.keys())
    pts = [yard] + [job_coords[p] for p in job_coords]
    hours, miles = ors_matrix(pts)
    time_lookup, mile_lookup = {}, {}
    for i, a in enumerate(labels):
        for j, b in enumerate(labels):
            time_lookup[(a, b)] = hours[i][j]
            mile_lookup[(a, b)] = miles[i][j]
    return time_lookup, mile_lookup

def fmt_travel_time(hours):
    if hours is None:
        return ""
    mins = int(round(float(hours) * 60))
    h, m = divmod(mins, 60)
    return f"{h} hr {m:02d} min" if h else f"{m} min"


def workdays(year,month,half,enabled_weekdays=None):
    if enabled_weekdays is None:
        enabled_weekdays={0,1,2,3,4}
    d=date(year,month,1); out=[]
    while d.month==month:
        if d.weekday() in enabled_weekdays and ((half==1 and d.day<=16) or (half==2 and d.day>=17)):
            out.append(d)
        d+=timedelta(days=1)
    return out

def enabled_weekday_set(day_flags):
    names=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    return {i for i,n in enumerate(names) if day_flags.get(n,False)}

def planning_dates_between(start_date,end_date,enabled_weekdays):
    out=[]; d=start_date
    while d<=end_date:
        if d.weekday() in enabled_weekdays:
            out.append(d)
        d+=timedelta(days=1)
    return out

def build_schedule(df, cols, teams, men, hours_day, yard_pc, enabled_weekdays=None):
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

    yard_key=re.sub(r"\s+","",str(yard_pc).upper())
    if yard_key=="ST161BQ":
        yard=(52.8186,-2.1190)
    else:
        yard=geocode_postcode(yard_pc)
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
    if failed:
        raise ValueError("Could not locate postcode(s): "+", ".join(failed[:12]))

    x["_coord"]=x["_pc"].map(coords)
    x["_yard_dist"]=x["_coord"].map(lambda c:hav(yard,c))

    with st.spinner("Getting real road mileage and travel times…"):
        road_time, road_miles = build_travel_lookup(yard, coords)

    scheduled=[]
    for half in [1,2]:
        pool=x[x["_half"]==half].sort_values("_yard_dist",ascending=False).copy()
        days=workdays(year,month,half,enabled_weekdays); day_i=0
        while len(pool):
            if day_i>=len(days):
                raise ValueError("Not enough working days to fit all jobs.")
            day=days[day_i]; used=set()
            for t in range(1,teams+1):
                avail=pool[~pool.index.isin(used)]
                if avail.empty: break

                seed=avail.sort_values("_yard_dist",ascending=False).iloc[0]
                route=[seed.name]
                first_pc=seed["_pc"]
                first_leg=road_time.get(("__YARD__",first_pc))
                if first_leg is None:
                    raise ValueError(f"No road route returned for yard to {first_pc}.")
                elapsed=first_leg+(seed["_hrs"]*0.80)/men
                current_pc=first_pc
                current_dist=seed["_yard_dist"]

                while True:
                    cand=pool[(~pool.index.isin(used|set(route)))&(pool["_yard_dist"]<=current_dist+8)]
                    if cand.empty: break
                    cand=cand.copy()
                    cand["_leg"]=cand["_pc"].map(lambda p: road_time.get((current_pc,p)))
                    cand=cand[cand["_leg"].notna()].sort_values(["_leg","_yard_dist"],ascending=[True,False])
                    picked=None
                    for idx,r in cand.iterrows():
                        return_leg=road_time.get((r["_pc"],"__YARD__"))
                        if return_leg is None: continue
                        trial=elapsed+r["_leg"]+(r["_hrs"]*0.80)/men+return_leg
                        if trial<=hours_day:
                            picked=(idx,r); break
                    if not picked: break
                    idx,r=picked
                    route.append(idx)
                    elapsed+=r["_leg"]+(r["_hrs"]*0.80)/men
                    current_pc=r["_pc"]
                    current_dist=r["_yard_dist"]

                return_hours=road_time.get((current_pc,"__YARD__"))
                return_miles=road_miles.get((current_pc,"__YARD__"))
                if return_hours is None:
                    raise ValueError(f"No road route returned from {current_pc} to yard.")
                elapsed+=return_hours

                prev_pc="__YARD__"
                total_miles=0.0
                total_travel_hours=0.0
                route_rows=[]
                for order,idx in enumerate(route,1):
                    r=pool.loc[idx]
                    leg_h=road_time.get((prev_pc,r["_pc"]))
                    leg_m=road_miles.get((prev_pc,r["_pc"]))
                    if leg_h is None or leg_m is None:
                        raise ValueError(f"No road route returned for leg to {r['_pc']}.")
                    total_miles+=leg_m
                    total_travel_hours+=leg_h
                    route_rows.append((order,idx,r,leg_h,leg_m))
                    prev_pc=r["_pc"]

                total_miles += return_miles or 0
                total_travel_hours += return_hours

                for order,idx,r,leg_h,leg_m in route_rows:
                    scheduled.append({
                        "Planned Date":day,"Team":f"Team {t}","Route Order":order,
                        "Job Number":r["_job"],"Site Name":r["_site"],"Post Code":r["_pc"],
                        "Due Date":r["_due"].date(),"Contract Hours":float(r["_hrs"]),
                        "Planned Hours (80%)":round(float(r["_hrs"])*0.80,2),
                        "On-Site Hours":round((float(r["_hrs"])*0.80)/men,2),
                        "Travel Miles":round(float(leg_m),1),
                        "Travel Time":fmt_travel_time(leg_h),
                        "Travel Time Hours":round(float(leg_h),2),
                        "Month Half":"1st Half" if half==1 else "2nd Half",
                        "Route Day Hours":round(elapsed,2),
                        "Daily Total Miles":round(total_miles,1),
                        "Daily Travel Time":fmt_travel_time(total_travel_hours),
                        "Return to Yard Miles":round(float(return_miles or 0),1),
                        "Return to Yard Time":fmt_travel_time(return_hours)
                    })
                used.update(route)
            pool=pool.drop(index=list(used)); day_i+=1
    return pd.DataFrame(scheduled).sort_values(["Planned Date","Team","Route Order"])


def build_client_planned_dates(df, cols, teams, men, hours_day, yard_pc, start_date, enabled_weekdays):
    """Plan client dates and return them in the exact original upload order."""
    if not enabled_weekdays:
        raise ValueError("Turn on at least one working day.")

    jc,sc,pc,dc,hc=[cols[k] for k in ["job","site","postcode","due","hours"]]
    x=df.copy()
    x["_orig_order"]=range(len(x))
    x["_job"]=x[jc].astype(str)
    x["_site"]=x[sc].astype(str).str.strip()
    x["_pc"]=x[pc].astype(str).str.upper().str.strip()
    x["_due"]=pd.to_datetime(x[dc],dayfirst=True,errors="coerce")
    x["_hrs"]=pd.to_numeric(x[hc],errors="coerce")
    bad=x[x["_due"].isna()|x["_hrs"].isna()|x["_pc"].isin(["","NAN","NONE"])]
    if len(bad):
        raise ValueError(f"{len(bad)} job(s) have missing/invalid due date, postcode or hours.")

    year=int(x["_due"].dt.year.mode().iloc[0]); month=int(x["_due"].dt.month.mode().iloc[0])
    x["_half"]=(x["_due"].dt.day>16).astype(int)+1

    yard_key=re.sub(r"\s+","",str(yard_pc).upper())
    yard=(52.8186,-2.1190) if yard_key=="ST161BQ" else geocode_postcode(yard_pc)
    if not yard:
        raise ValueError("Could not locate the yard postcode.")

    coords={}
    prog=st.progress(0,text="Locating postcodes for client dates…")
    pcs=x["_pc"].unique()
    for i,p in enumerate(pcs):
        coords[p]=geocode_postcode(p)
        prog.progress((i+1)/len(pcs),text=f"Locating postcodes… {i+1}/{len(pcs)}")
    prog.empty()
    failed=[p for p,v in coords.items() if not v]
    if failed:
        raise ValueError("Could not locate postcode(s): "+", ".join(failed[:12]))

    x["_coord"]=x["_pc"].map(coords)
    x["_yard_dist"]=x["_coord"].map(lambda c:hav(yard,c))
    with st.spinner("Getting real road mileage and travel times for client dates…"):
        road_time, road_miles = build_travel_lookup(yard,coords)

    assignments={}
    daily={}      # (date,team) -> route state
    site_dates={} # site key -> assigned dates

    def skey(row):
        s=norm(row["_site"])
        return s if s else norm(row["_pc"])

    def spread_score(row,d):
        prior=site_dates.get(skey(row),[])
        if not prior:
            return 999
        return min(abs((d-p).days) for p in prior)

    def try_add(idx,row,d,t):
        key=(d,t)
        slot=daily.setdefault(key,{"elapsed":0.0,"current":"__YARD__","jobs":[]})
        leg=road_time.get((slot["current"],row["_pc"]))
        ret=road_time.get((row["_pc"],"__YARD__"))
        if leg is None or ret is None:
            return False
        onsite=(float(row["_hrs"])*0.80)/men
        trial=slot["elapsed"]+leg+onsite+ret
        if trial>hours_day:
            return False
        slot["elapsed"] += leg+onsite
        slot["current"]=row["_pc"]
        slot["jobs"].append(idx)
        assignments[idx]=d
        site_dates.setdefault(skey(row),[]).append(d)
        return True

    month_start=date(year,month,1)
    first_end=date(year,month,16)
    fh_start=max(start_date,month_start)

    # First half: forwards from selected planning start date, never after each job's due date.
    first_days=planning_dates_between(fh_start,first_end,enabled_weekdays)
    first=x[x["_half"]==1].sort_values(["_due","_yard_dist"],ascending=[True,False])
    for idx,row in first.iterrows():
        candidates=[d for d in first_days if d<=row["_due"].date()]
        # Earlier dates first, but prefer separation from repeat visits.
        candidates=sorted(candidates,key=lambda d:(d.toordinal(),-spread_score(row,d)))
        placed=False
        for d in candidates:
            for t in range(1,teams+1):
                if try_add(idx,row,d,t):
                    placed=True; break
            if placed: break
        if not placed:
            raise ValueError(f"Could not fit first-half job {row['_job']} by {row['_due'].date():%d/%m/%Y}.")

    # Second half: backwards from each job's due date.
    second_floor=max(date(year,month,17),fh_start)
    second=x[x["_half"]==2].sort_values(["_due","_yard_dist"],ascending=[False,False])
    for idx,row in second.iterrows():
        candidates=planning_dates_between(second_floor,row["_due"].date(),enabled_weekdays)
        candidates=sorted(candidates,key=lambda d:(-d.toordinal(),-spread_score(row,d)))
        placed=False
        for d in candidates:
            for t in range(1,teams+1):
                if try_add(idx,row,d,t):
                    placed=True; break
            if placed: break
        if not placed:
            raise ValueError(f"Could not fit second-half job {row['_job']} working backwards from {row['_due'].date():%d/%m/%Y}.")

    result=x[["_orig_order","_job","_site","_pc","_due"]].copy()
    result["Planned Date"]=result.index.map(assignments)
    result=result.sort_values("_orig_order")
    result["Planned Date"]=pd.to_datetime(result["Planned Date"]).dt.date
    return result.rename(columns={
        "_job":"Job Number","_site":"Site Name","_pc":"Post Code","_due":"Due Date"
    })[["Job Number","Site Name","Post Code","Due Date","Planned Date"]]

def make_client_dates_excel(client_dates):
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as w:
        client_dates.to_excel(w,index=False,sheet_name="Client Planned Dates")
        client_dates[["Planned Date"]].to_excel(w,index=False,sheet_name="Paste Planned Dates")
    return buf.getvalue()

def make_pdf(plan,yard):
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=landscape(A4),rightMargin=7*mm,leftMargin=7*mm,topMargin=8*mm,bottomMargin=8*mm)
    styles=getSampleStyleSheet()
    title=ParagraphStyle("t",parent=styles["Title"],fontSize=18,alignment=TA_CENTER)
    small=ParagraphStyle("s",parent=styles["Normal"],fontSize=8)
    cell=ParagraphStyle("c",parent=styles["Normal"],fontSize=7.5,leading=8.5)
    story=[]
    teams=list(plan["Team"].drop_duplicates())
    for ti,team in enumerate(teams):
        story += [Paragraph(f"{team} - Monthly Job List",title),
                  Paragraph(f"Start/finish yard: {yard} | Travel shown is from the previous stop",small),Spacer(1,4*mm)]
        for d,g in plan[plan["Team"]==team].groupby("Planned Date",sort=True):
            g=g.sort_values("Route Order")
            story.append(Paragraph(pd.Timestamp(d).strftime("%A %d/%m/%Y"),styles["Heading2"]))
            data=[["Done","Order","Job No.","Site","Postcode","Due By","Contract Hrs","Planned Hrs (80%)","On-Site Hrs","Travel Miles","Travel Time"]]
            for _,r in g.iterrows():
                data.append(["☐",str(int(r["Route Order"])),str(r["Job Number"]),Paragraph(str(r["Site Name"]),cell),
                             str(r["Post Code"]),pd.Timestamp(r["Due Date"]).strftime("%d/%m/%Y"),
                             f'{r["Contract Hours"]:.2f}',f'{r["Planned Hours (80%)"]:.2f}',f'{r["On-Site Hours"]:.2f}',f'{r["Travel Miles"]:.1f}',str(r["Travel Time"])])
            tb=Table(data,colWidths=[8*mm,9*mm,17*mm,48*mm,18*mm,18*mm,18*mm,24*mm,18*mm,20*mm,24*mm],repeatRows=1)
            tb.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1F4E78")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),7.5),
                ("GRID",(0,0),(-1,-1),0.4,colors.HexColor("#AAAAAA")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                ("ALIGN",(0,0),(2,-1),"CENTER"),("ALIGN",(4,1),(-1,-1),"CENTER"),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F4F6F8")]),
                ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
            last=g.iloc[-1]
            summary=(f'Return to ST16 1BQ: {last["Return to Yard Miles"]:.1f} miles · {last["Return to Yard Time"]}'
                     f'    |    Daily total: {last["Daily Total Miles"]:.1f} miles · {last["Daily Travel Time"]}'
                     f'    |    Total day hours: {last["Route Day Hours"]:.2f} hrs')
            story += [tb,Spacer(1,2*mm),Paragraph(summary,small),Spacer(1,4*mm)]
        story += [Paragraph("Notes: ______________________________________________________________________________________________________",small),
                  Spacer(1,3*mm),Paragraph("Operative signature: ____________________________________    Date: ____________________",small)]
        if ti<len(teams)-1: story.append(PageBreak())
    doc.build(story)
    return buf.getvalue()

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
    st.subheader("Working days")
    defaults={"Monday":True,"Tuesday":True,"Wednesday":True,"Thursday":True,"Friday":True,"Saturday":False,"Sunday":False}
    day_flags={d:st.checkbox(d,value=v,key=f"workday_{d}") for d,v in defaults.items()}
    enabled_weekdays=enabled_weekday_set(day_flags)
    st.info(f"Capacity: {int(teams)} teams × {int(men)} men × {hours:g} hours")
    st.caption("Jobs are planned at 80% of Contract Hours. Full Contract Hours remain visible.")

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

    st.divider()
    st.subheader("Client planned dates")
    st.caption("First-half work plans forwards; second-half work plans backwards. Results return in the exact original Excel row order.")
    due_preview=pd.to_datetime(df[cols["due"]],dayfirst=True,errors="coerce").dropna()
    default_start=due_preview.min().date().replace(day=1) if len(due_preview) else date.today()
    planning_start=st.date_input("Planning start date",value=default_start,key="client_planning_start")
    if st.button("Generate client planned dates"):
        try:
            client_dates=build_client_planned_dates(
                df,cols,int(teams),int(men),float(hours),yard,planning_start,enabled_weekdays
            )
            st.session_state["client_dates"]=client_dates
        except Exception as e:
            st.error(str(e))

    st.divider()
    if st.button("Generate monthly plan",type="primary"):
        try:
            plan=build_schedule(df,cols,int(teams),int(men),float(hours),yard,enabled_weekdays)
            st.session_state["plan"]=plan; st.session_state["yard"]=yard
        except Exception as e:
            st.error(str(e))

if "client_dates" in st.session_state:
    client_dates=st.session_state["client_dates"]
    st.subheader("Client planned dates — original Excel row order")
    st.dataframe(client_dates,use_container_width=True)
    st.download_button(
        "Download client planned dates",
        make_client_dates_excel(client_dates),
        "NI_Client_Planned_Dates.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )
    st.caption("The 'Paste Planned Dates' sheet contains one Planned Date column in the exact uploaded row order.")

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
    st.caption("Travel mileage and time are calculated using the connected OpenRouteService road-routing matrix.")
