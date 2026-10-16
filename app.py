"""Streamlit dashboard for outreach.py.  Run:  streamlit run app.py"""
import datetime as dt
import streamlit as st
import outreach as o

st.set_page_config(page_title="Outreach", page_icon="📬", layout="centered")
st.title("📬 Outreach")

# ------------------------------------------------------------ sidebar: find + draft
with st.sidebar:
    st.header("Find new prospects")
    if not o.has_key():
        st.error("Add your GROQ_API_KEY to the .env file, then restart.")
    place = st.text_input("City, country", "Lagos, Nigeria", help="Any city worldwide, e.g. Paris, France")
    segment = st.text_input("Business type", "restaurants",
                            help="Pick one: " + ", ".join(o.SEGMENTS) + ". Or a raw tag like tag:amenity=bar")
    try:
        seg_name, _ = o.resolve_segment(segment)
    except ValueError as e:
        seg_name = ""
        st.warning(str(e))
    limit = st.number_input("How many businesses", 5, 100, 15, help="15 is plenty for a day. More = slower (free AI rate limits).")
    if st.button("Find & draft messages", type="primary"):
        try:
            bar = st.progress(0.0, text="Finding businesses on the map...")
            n = o.cmd_source(place, segment, "", int(limit))
            if n:
                o.cmd_generate(progress=lambda d, t: bar.progress(d / t, text=f"Researching + drafting {d}/{t}"))
            bar.empty()
            if n == 0:
                st.warning("No new businesses with contact details found. Try another city or type.")
            else:
                st.rerun()
        except Exception as e:
            st.error(str(e))
    st.caption(f"Daily send cap: {o.DAILY_CAP}")
    with st.expander(f"✏️ Edit my offer for: {seg_name or 'general'}"):
        if seg_name and not o.has_segment_offer(seg_name):
            st.warning(f"No saved offer for '{seg_name}' yet. Write one before drafting.")
        new_offer = st.text_area("What you're selling here (price + terms)", o.get_offer(seg_name), height=220)
        if st.button("Save offer"):
            o.save_offer(new_offer, seg_name); st.success("Saved. Applies to the next drafts.")

rows = o.load()
today = str(dt.date.today())
sent_today = sum(1 for r in rows if r["sent_date"] == today)
room = max(0, o.DAILY_CAP - sent_today)
new = sorted([r for r in rows if r["status"] == "new"],
             key=lambda r: -int(r["fit_score"] or 0))[:room]
due = [r for r in rows if r["status"] == "sent" and r["next_action"] and r["next_action"] <= today]

t1, t2, t3, t4 = st.tabs([f"Send ({len(new)})", f"Follow-ups ({len(due)})", "Pipeline", "Stats"])

# ------------------------------------------------------------ send queue
with t1:
    st.caption(f"Sent today: {sent_today}/{o.DAILY_CAP}. Edit the text, tap send, then tap Done.")
    if not new:
        st.info("Nothing queued. Use 'Find & draft messages' in the sidebar.")
    for r in new:
        with st.expander(f"{r['firm_name']}  ·  {r['segment']}  ·  fit {r['fit_score']}  ·  {r['country']}"):
            st.write(f"**Hook:** {r['hook']}")
            st.write(f"{r['contact_name'] or 'No name found'} · {r['email'] or 'no email'} · {r['phone'] or 'no phone'}")
            if r["email"]:
                subj = st.text_input("Subject", r["subject"], key=f"s{r['id']}")
                body = st.text_area("Email", r["email_body"], height=200, key=f"b{r['id']}")
                st.link_button("✉️ Open in email app", o.mailto(r["email"], subj, body))
            if r["phone"]:
                wa = st.text_area("WhatsApp", r["whatsapp_msg"], height=100, key=f"w{r['id']}")
                link = o.wa_link(r["phone"], wa)
                if link:
                    st.link_button("💬 Open WhatsApp", link)
            c1, c2 = st.columns(2)
            if c1.button("✅ Done, I sent it", key=f"d{r['id']}"):
                o.cmd_sent(r["id"]); st.rerun()
            if c2.button("⏭ Skip", key=f"k{r['id']}"):
                o.cmd_mark(r["id"], "nofit"); st.rerun()

# ------------------------------------------------------------ follow-ups
with t2:
    if not due:
        st.info("No follow-ups due today.")
    for r in due:
        n = int(r["followups_sent"] or 0)
        text = r["followup1"] if n == 0 else r["followup2"]
        with st.expander(f"{r['firm_name']}  ·  follow-up {n + 1}"):
            msg = st.text_area("Message", text, height=120, key=f"f{r['id']}")
            if r["email"]:
                st.link_button("✉️ Reply by email", o.mailto(r["email"], "re: " + r["subject"], msg))
            if r["phone"] and o.wa_link(r["phone"], msg):
                st.link_button("💬 WhatsApp", o.wa_link(r["phone"], msg))
            if st.button("✅ Done, I sent it", key=f"fd{r['id']}"):
                o.cmd_sent(r["id"]); st.rerun()

# ------------------------------------------------------------ pipeline
with t3:
    contacted = [r for r in rows if r["sent_date"]]
    st.caption("Update a prospect when they reply.")
    for r in contacted:
        c1, c2, c3 = st.columns([3, 2, 1])
        c1.write(f"**{r['firm_name']}**  \n{r['status']} · sent {r['sent_date']}")
        s = c2.selectbox("Status", ["sent", "replied", "call", "won", "lost", "closed_no_reply"],
                         index=["sent", "replied", "call", "won", "lost", "closed_no_reply"].index(r["status"])
                         if r["status"] in ["sent", "replied", "call", "won", "lost", "closed_no_reply"] else 0,
                         key=f"p{r['id']}", label_visibility="collapsed")
        if c3.button("Save", key=f"ps{r['id']}") and s != r["status"]:
            o.cmd_mark(r["id"], s); st.rerun()
    if not contacted:
        st.info("Nobody contacted yet.")

# ------------------------------------------------------------ stats
with t4:
    touched = [r for r in rows if r["sent_date"]]
    replied = [r for r in touched if r["status"] in ("replied", "call", "won", "lost")]
    calls = [r for r in touched if r["status"] in ("call", "won")]
    won = [r for r in touched if r["status"] == "won"]
    a, b, c, d = st.columns(4)
    a.metric("Contacted", len(touched))
    b.metric("Replies", len(replied), f"{100 * len(replied) / (len(touched) or 1):.1f}%")
    c.metric("Calls", len(calls))
    d.metric("Won", len(won))
    st.caption("After 50 sends: under 2% reply = fix the offer/targeting, not volume. 2-5% normal. Over 5% scale.")
