import streamlit as st
import pandas as pd
import sqlite3
from io import BytesIO
from datetime import datetime, timedelta
import bcrypt
import plotly.express as px
import plotly.graph_objects as go

try:
    from fpdf import FPDF
    USE_FPDF = True
except ImportError:
    USE_FPDF = False


# ─── Database Setup ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect('loans.db')
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS loans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sn INTEGER,
        names TEXT,
        date TEXT,
        amount REAL,
        int_rate REAL,
        duration INTEGER,
        admin_fees REAL,
        interest REAL,
        penalty_charged REAL,
        total REAL,
        g_total REAL,
        amt_remitted REAL,
        balance REAL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        date TEXT NOT NULL,
        description TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS other_income (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        date TEXT NOT NULL,
        description TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'")
    if c.fetchone()[0] == 0:
        hashed = bcrypt.hashpw("password".encode('utf-8'), bcrypt.gensalt())
        c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("admin", hashed))

    conn.commit()
    conn.close()


@st.cache_data(ttl=10)
def load_loans_df():
    conn = sqlite3.connect('loans.db')
    df = pd.read_sql_query("SELECT * FROM loans ORDER BY sn ASC", conn)
    conn.close()
    if df.empty:
        return pd.DataFrame(columns=['id','sn','names','date','amount','int_rate','duration','admin_fees','interest','penalty_charged','total','g_total','amt_remitted','balance'])
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    return df


@st.cache_data(ttl=10)
def load_expenses_df():
    conn = sqlite3.connect('loans.db')
    df = pd.read_sql_query("SELECT * FROM expenses ORDER BY date DESC", conn)
    conn.close()
    if df.empty:
        return pd.DataFrame(columns=['id','category','amount','date','description'])
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    return df


@st.cache_data(ttl=10)
def load_other_income_df():
    conn = sqlite3.connect('loans.db')
    df = pd.read_sql_query("SELECT * FROM other_income ORDER BY date DESC", conn)
    conn.close()
    if df.empty:
        return pd.DataFrame(columns=['id','category','amount','date','description'])
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    return df


@st.cache_data(ttl=10)
def load_users_df():
    conn = sqlite3.connect('loans.db')
    df = pd.read_sql_query("SELECT id, username, created_at FROM users ORDER BY created_at DESC", conn)
    conn.close()
    return df


def save_loans_df(df):
    df_save = df.copy()
    if 'date' in df_save.columns and pd.api.types.is_datetime64_any_dtype(df_save['date']):
        df_save['date'] = df_save['date'].dt.strftime('%Y-%m-%d').where(df_save['date'].notna(), None)
    conn = sqlite3.connect('loans.db')
    df_save.to_sql('loans', conn, if_exists='replace', index=False)
    conn.close()


def save_expense(category, amount, date_str, description=""):
    conn = sqlite3.connect('loans.db')
    c = conn.cursor()
    c.execute("INSERT INTO expenses (category, amount, date, description) VALUES (?, ?, ?, ?)",
              (category, amount, date_str, description))
    conn.commit()
    conn.close()


def save_other_income(category, amount, date_str, description=""):
    conn = sqlite3.connect('loans.db')
    c = conn.cursor()
    c.execute("INSERT INTO other_income (category, amount, date, description) VALUES (?, ?, ?, ?)",
              (category, amount, date_str, description))
    conn.commit()
    conn.close()


def add_new_user(username, password):
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    conn = sqlite3.connect('loans.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, hashed))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def delete_user(user_id, current_username):
    conn = sqlite3.connect('loans.db')
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ? AND username != ? AND username != 'admin'", (user_id, current_username))
    conn.commit()
    conn.close()


def verify_login(username, password):
    conn = sqlite3.connect('loans.db')
    c = conn.cursor()
    c.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    result = c.fetchone()
    conn.close()
    if result:
        return bcrypt.checkpw(password.encode('utf-8'), result[0])
    return False


# ─── Loan Calculations ─────────────────────────────────────────────────────────
def months_overdue(loan_date, duration_months):
    try:
        if isinstance(loan_date, str):
            start = datetime.strptime(loan_date, "%Y-%m-%d")
        else:
            start = loan_date
        due = start + timedelta(days=int(duration_months * 30.437))
        today = datetime.now()
        if today <= due:
            return 0
        delta_days = (today - due).days
        return delta_days // 30
    except:
        return 0


def calculate_loan_fields(amount, rate, duration, admin_fees, remitted, loan_date):
    interest = amount * (rate / 100) * duration
    overdue_months = months_overdue(loan_date, duration) if pd.notna(loan_date) else 0
    penalty = amount * 0.10 * overdue_months

    provisional = amount + admin_fees + interest - remitted
    if provisional <= 0:
        penalty = 0.0

    total_add = admin_fees + interest + penalty
    g_total = amount + total_add
    balance = g_total - remitted

    return (
        round(interest, 2),
        round(penalty, 2),
        round(total_add, 2),
        round(g_total, 2),
        round(max(balance, 0), 2)
    )


# ─── PDF Functions ─────────────────────────────────────────────────────────────
def generate_fancy_pdf_single_client(row_or_df):
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()

    try:
        pdf.image("logo.jpg", x=10, y=8, w=40)
    except:
        pass

    pdf.set_font("Arial", "B", 18)
    pdf.set_text_color(0, 51, 102)
    pdf.cell(0, 14, "TRUSTED FRIENDS LIMITED", ln=1, align='C')
    pdf.set_font("Arial", "I", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, "Reliable Loan Management & Financial Services", ln=1, align='C')

    pdf.set_fill_color(0, 102, 204)
    pdf.rect(0, 45, 210, 38, 'F')
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Arial", "B", 22)
    pdf.cell(0, 22, "LOAN STATEMENT", ln=1, align='C')
    pdf.set_font("Arial", "", 11)
    pdf.cell(0, 10, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=1, align='C')

    pdf.ln(10)

    if isinstance(row_or_df, pd.DataFrame):
        if row_or_df.empty:
            pdf.set_text_color(200, 0, 0)
            pdf.set_font("Arial", "B", 14)
            pdf.cell(0, 12, "No loan record found.", ln=1, align='C')
            buf = BytesIO()
            pdf.output(buf)
            buf.seek(0)
            return buf
        row = row_or_df.iloc[0]
    else:
        row = row_or_df

    date_display = row['date'].strftime("%Y-%m-%d") if pd.notnull(row['date']) else "—"

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 12, f"Client: {row['names']}", ln=1)
    pdf.set_font("Arial", "", 11)
    pdf.cell(0, 10, f"Disbursement Date: {date_display}", ln=1)

    pdf.ln(10)

    pdf.set_fill_color(240, 248, 255)
    pdf.rect(10, pdf.get_y(), 190, 60, 'F')
    pdf.set_xy(15, pdf.get_y() + 6)

    pdf.set_font("Arial", "B", 12)
    pdf.cell(65, 9, "Principal Amount:", 0, 0)
    pdf.cell(0, 9, f"NGN {row['amount']:,.2f}", ln=1)

    pdf.cell(65, 9, "Total Interest:", 0, 0)
    pdf.cell(0, 9, f"NGN {row['interest']:,.2f}", ln=1)

    pdf.cell(65, 9, "Penalty Accumulated:", 0, 0)
    pdf.cell(0, 9, f"NGN {row['penalty_charged']:,.2f}", ln=1)

    pdf.cell(65, 9, "Grand Total Due:", 0, 0)
    pdf.cell(0, 9, f"NGN {row['g_total']:,.2f}", ln=1)

    pdf.set_font("Arial", "B", 13)
    pdf.set_text_color(220, 53, 69)
    pdf.cell(65, 10, "Outstanding Balance:", 0, 0)
    pdf.cell(0, 10, f"NGN {row['balance']:,.2f}", ln=1)

    pdf.set_text_color(0, 0, 0)
    pdf.ln(25)

    pdf.set_font("Arial", "B", 11)
    pdf.set_fill_color(245, 245, 245)
    headers = ["SN", "Date", "Int. Rate", "Duration", "Admin Fee", "Remitted", "Balance"]
    widths = [15, 30, 28, 25, 30, 30, 35]

    for i, h in enumerate(headers):
        pdf.cell(widths[i], 10, h, 1, 0, 'C', True)
    pdf.ln()

    pdf.set_font("Arial", "", 10)
    pdf.cell(widths[0], 10, str(row['sn']), 1, 0, 'C')
    pdf.cell(widths[1], 10, date_display, 1, 0, 'C')
    pdf.cell(widths[2], 10, f"{row['int_rate']}%", 1, 0, 'C')
    pdf.cell(widths[3], 10, str(row['duration']), 1, 0, 'C')
    pdf.cell(widths[4], 10, f"NGN {row['admin_fees']:,.2f}", 1, 0, 'C')
    pdf.cell(widths[5], 10, f"NGN {row['amt_remitted']:,.2f}", 1, 0, 'C')
    pdf.cell(widths[6], 10, f"NGN {row['balance']:,.2f}", 1, 1, 'C')

    pdf.ln(35)
    pdf.set_font("Arial", "", 11)
    pdf.cell(95, 10, "___________________________", 0, 0, 'L')
    pdf.cell(95, 10, "___________________________", 0, 1, 'R')
    pdf.cell(95, 8, "Chairman", 0, 0, 'L')
    pdf.cell(95, 8, "Secretary", 0, 1, 'R')

    buf = BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf


def generate_profit_loss_pdf(pl_data, period_text):
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()

    try:
        pdf.image("logo.jpg", x=10, y=8, w=40)
    except:
        pass

    pdf.set_font("Arial", "B", 18)
    pdf.set_text_color(0, 51, 102)
    pdf.cell(0, 14, "TRUSTED FRIENDS LIMITED", ln=1, align='C')
    pdf.set_font("Arial", "I", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, "Accurate Financial Reporting & Loan Management", ln=1, align='C')

    pdf.set_fill_color(0, 102, 204)
    pdf.rect(0, 55, 210, 35, 'F')
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Arial", "B", 20)
    pdf.cell(0, 18, "PROFIT & LOSS STATEMENT", ln=1, align='C')
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 10, f"Period: {period_text}", ln=1, align='C')
    pdf.cell(0, 8, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=1, align='C')

    pdf.ln(15)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 12, "INCOME (Revenue)", ln=1)
    pdf.set_font("Arial", "", 12)

    pdf.cell(140, 10, "Interest Income", border=1)
    pdf.cell(50, 10, f"NGN {pl_data['interest']:,.2f}", border=1, ln=1, align='R')

    pdf.cell(140, 10, "Admin / Processing Fees", border=1)
    pdf.cell(50, 10, f"NGN {pl_data['admin_fees']:,.2f}", border=1, ln=1, align='R')

    pdf.cell(140, 10, "Penalty Income", border=1)
    pdf.cell(50, 10, f"NGN {pl_data['penalty']:,.2f}", border=1, ln=1, align='R')

    for cat, amt in pl_data['other_income'].items():
        pdf.cell(140, 10, cat, border=1)
        pdf.cell(50, 10, f"NGN {amt:,.2f}", border=1, ln=1, align='R')

    pdf.set_font("Arial", "B", 13)
    pdf.cell(140, 12, "TOTAL REVENUE", border=1)
    pdf.cell(50, 12, f"NGN {pl_data['total_revenue']:,.2f}", border=1, ln=1, align='R')

    pdf.ln(10)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 12, "OPERATING EXPENSES", ln=1)
    pdf.set_font("Arial", "", 12)

    for cat, amt in pl_data['expenses'].items():
        pdf.cell(140, 10, cat, border=1)
        pdf.cell(50, 10, f"NGN {amt:,.2f}", border=1, ln=1, align='R')

    pdf.set_font("Arial", "B", 13)
    pdf.cell(140, 12, "TOTAL EXPENSES", border=1)
    pdf.cell(50, 12, f"NGN {pl_data['total_expenses']:,.2f}", border=1, ln=1, align='R')

    pdf.ln(12)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 12, "NET RESULT", ln=1)

    pdf.set_font("Arial", "B", 15)
    net_op = pl_data['net_operating_profit']
    color = (0, 128, 0) if net_op >= 0 else (200, 0, 0)
    pdf.set_text_color(*color)
    pdf.cell(0, 12, f"Net Operating Profit / (Loss): NGN {net_op:,.2f}", ln=1, align='C')

    pdf.ln(8)
    pdf.set_text_color(0, 102, 204)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 12, f"Owner's Equity Contribution: NGN {pl_data['equity_contribution']:,.2f}", ln=1, align='C')

    pdf.ln(8)
    pdf.set_font("Arial", "B", 16)
    final = net_op + pl_data['equity_contribution']
    color = (0, 128, 0) if final >= 0 else (200, 0, 0)
    pdf.set_text_color(*color)
    pdf.cell(0, 15, f"Final Net Position: NGN {final:,.2f}", ln=1, align='C')

    pdf.ln(40)
    pdf.set_font("Arial", "", 11)
    pdf.cell(95, 10, "___________________________", 0, 0, 'L')
    pdf.cell(95, 10, "___________________________", 0, 1, 'R')
    pdf.cell(95, 8, "Chairman", 0, 0, 'L')
    pdf.cell(95, 8, "Secretary", 0, 1, 'R')

    buf = BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf


# ─── App Initialization ────────────────────────────────────────────────────────
init_db()

st.set_page_config(page_title="Loan Management", layout="wide", page_icon="💰")

# Force scrollable layout + fix button visibility
st.markdown(
    """
    <style>
        .main .block-container {
            padding-top: 2rem !important;
            padding-bottom: 12rem !important;
            max-height: 100vh !important;
            overflow-y: auto !important;
        }
        .stTabs [data-baseweb="tab-panel"] {
            overflow-y: auto !important;
            max-height: 75vh !important;
            min-height: 400px !important;
        }
        .stForm {
            overflow-y: visible !important;
            max-height: none !important;
            padding-bottom: 8rem !important;
            margin-bottom: 4rem !important;
        }
        section[data-testid="stSidebar"] > div:first-child {
            overflow-y: auto !important;
            max-height: 100vh !important;
        }
        .stApp { overflow-y: auto !important; }
        footer { visibility: hidden; height: 0 !important; }
    </style>
    """,
    unsafe_allow_html=True
)

# Session state initialization
for key in ['general_auth', 'add_loan_auth', 'edit_loan_auth', 'admin_auth']:
    if key not in st.session_state:
        st.session_state[key] = False

if 'current_user' not in st.session_state:
    st.session_state.current_user = None

if 'df' not in st.session_state:
    st.session_state.df = load_loans_df()

if 'page' not in st.session_state:
    st.session_state.page = "Dashboard"


# ─── Auth Helpers ──────────────────────────────────────────────────────────────
def general_login_form():
    st.sidebar.header("Admin Login (for Reports & Restricted Sections)")
    u = st.sidebar.text_input("Username", key="general_u")
    p = st.sidebar.text_input("Password", type="password", key="general_p")
    if st.sidebar.button("Login", type="primary"):
        if verify_login(u.strip(), p.strip()):
            st.session_state.general_auth = True
            st.session_state.current_user = u.strip()
            st.rerun()
        else:
            st.sidebar.error("Invalid credentials")


def section_login_form(section_name, session_key):
    st.header(f"Login Required - {section_name}")
    st.info("This section requires admin authentication.")
    col1, col2 = st.columns([3, 1])
    with col1:
        u = st.text_input("Username", key=f"u_{session_key}")
        p = st.text_input("Password", type="password", key=f"p_{session_key}")
        if st.button("Login to Access", type="primary", key=f"login_{session_key}"):
            if verify_login(u.strip(), p.strip()):
                st.session_state[session_key] = True
                st.session_state.current_user = u.strip()
                st.rerun()
            else:
                st.error("Invalid username or password")


def logout_button():
    if st.button("Logout", type="primary"):
        for k in list(st.session_state.keys()):
            if 'auth' in k or k == 'current_user':
                del st.session_state[k]
        st.rerun()


# ─── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    if st.session_state.general_auth:
        st.success(f"Logged in as: **{st.session_state.current_user}**")
        logout_button()

    st.header("Navigation")
    pages = {
        "Dashboard": "Dashboard",
        "View Records": "View Records",
        "Reports": "Reports"
    }
    if st.session_state.general_auth:
        pages.update({
            "Add Loan": "Add Loan",
            "Edit Loans": "Edit Loans",
            "Admin Controls": "Admin Controls"
        })

    selected = st.selectbox("Section", list(pages.keys()))
    st.session_state.page = pages[selected]

    st.markdown("---")
    if st.button("Refresh Data"):
        st.session_state.df = load_loans_df()
        st.success("Data refreshed")
        st.rerun()


# ─── App Header with Logo ──────────────────────────────────────────────────────
col_logo, col_title = st.columns([1, 5])

with col_logo:
    try:
        st.image("logo.jpg", width=90)
    except FileNotFoundError:
        st.warning("logo.jpg not found – using placeholder")
        st.image("https://via.placeholder.com/90x90?text=Logo", width=90)

with col_title:
    st.title("TRUSTED FRIENDS VENTURES LIMITED")
    st.caption("Loan Management System")

df = st.session_state.df.copy()


# ─── Page Routing ──────────────────────────────────────────────────────────────
if st.session_state.page == "Dashboard":
    if not df.empty:
        # ── Fancy KPI Cards ────────────────────────────────────────────────────
        st.markdown("### Key Performance Indicators")
        kpi_cols = st.columns(4)

        total_principal = df['amount'].sum()
        total_balance   = df['balance'].sum()
        total_interest  = df['interest'].sum()
        total_paid      = df['amt_remitted'].sum()

        def fancy_metric(col, label, value, delta=None, color="#1f77b4"):
            with col:
                st.markdown(
                    f"""
                    <div style="
                        background: linear-gradient(135deg, {color}22, #ffffff);
                        border-radius: 16px;
                        padding: 1.4rem 1rem;
                        text-align: center;
                        box-shadow: 0 6px 20px rgba(0,0,0,0.08);
                        border: 1px solid rgba(0,0,0,0.05);
                        transition: transform 0.2s;
                    " onmouseover="this.style.transform='scale(1.03)'" onmouseout="this.style.transform='scale(1)'">
                        <div style="font-size: 1.1rem; color: #555; margin-bottom: 0.5rem; font-weight: 500;">{label}</div>
                        <div style="font-size: 2.2rem; font-weight: bold; color: {color};">NGN {value:,.0f}</div>
                        {f'<div style="font-size: 0.95rem; color: #28a745; margin-top: 0.4rem;">{delta}</div>' if delta else ''}
                    </div>
                    """,
                    unsafe_allow_html=True
                )

        fancy_metric(kpi_cols[0], "Total Principal Disbursed", total_principal, color="#27ae60")
        fancy_metric(kpi_cols[1], "Total Outstanding Balance", total_balance,
                     delta=f"↑ {total_balance/total_principal*100:.1f}% of principal" if total_principal > 0 else None,
                     color="#c0392b")
        fancy_metric(kpi_cols[2], "Total Interest Earned", total_interest, color="#2980b9")
        fancy_metric(kpi_cols[3], "Total Amount Repaid", total_paid, color="#8e44ad")

        # ── Visual Insights Tabs ───────────────────────────────────────────────
        st.markdown("### Visual Insights")
        tab1, tab2, tab3 = st.tabs(["Balance Trend Over Time", "Outstanding by Client", "Balance Distribution"])

        # ── Tab 1: Time Trend ──────────────────────────────────────────────────
        with tab1:
            df_trend = df.copy()
            df_trend['month'] = df_trend['date'].dt.to_period('M').astype(str)
            monthly_balance = df_trend.groupby('month')['balance'].sum().reset_index()
            monthly_principal = df_trend.groupby('month')['amount'].sum().reset_index()

            fig_trend = go.Figure()

            fig_trend.add_trace(
                go.Scatter(
                    x=monthly_balance['month'],
                    y=monthly_balance['balance'],
                    mode='lines+markers',
                    name='Outstanding Balance',
                    fill='tozeroy',
                    fillcolor='rgba(231, 76, 60, 0.18)',
                    line=dict(color='#e74c3c', width=3),
                    marker=dict(size=8, color='#c0392b')
                )
            )

            fig_trend.add_trace(
                go.Scatter(
                    x=monthly_principal['month'],
                    y=monthly_principal['amount'],
                    mode='lines+markers',
                    name='Principal Disbursed',
                    line=dict(color='#27ae60', width=2, dash='dot'),
                    marker=dict(size=6, color='#2ecc71')
                )
            )

            fig_trend.update_layout(
                title="Monthly Trend: Outstanding Balance vs Disbursed Principal",
                xaxis_title="Month",
                yaxis_title="Amount (NGN)",
                hovermode="x unified",
                plot_bgcolor='rgba(0,0,0,0)',
                paper_bgcolor='rgba(0,0,0,0)',
                font=dict(family="Segoe UI, Arial", size=13),
                title_font=dict(size=22),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                height=550,
                margin=dict(l=40, r=40, t=80, b=60)
            )

            st.plotly_chart(fig_trend, use_container_width=True, config={'displayModeBar': False})

        # ── Tab 2: Bar chart ───────────────────────────────────────────────────
        with tab2:
            fig_bar = px.bar(
                df.sort_values('balance', ascending=False).head(15),
                x='names',
                y='balance',
                title="Top Clients by Outstanding Balance",
                labels={'names': 'Client Name', 'balance': 'Balance Due (NGN)'},
                color='balance',
                color_continuous_scale='RdYlGn_r',
                text_auto='.0f',
                height=520
            )
            fig_bar.update_traces(
                textposition='auto',
                marker_line_color='black',
                marker_line_width=1,
                hovertemplate='<b>%{x}</b><br>Balance: NGN %{y:,.0f}<extra></extra>'
            )
            fig_bar.update_layout(
                xaxis_title="Client Name",
                yaxis_title="Balance Due (NGN)",
                plot_bgcolor='rgba(0,0,0,0)',
                paper_bgcolor='rgba(0,0,0,0)',
                font=dict(family="Segoe UI, Arial", size=13),
                title_font=dict(size=22),
                bargap=0.22,
                showlegend=False
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        # ── Tab 3: Pie chart ───────────────────────────────────────────────────
        with tab3:
            fig_pie = px.pie(
               df[df['balance'] > 0],
               values='balance',
               names='names',
               title="Distribution of Outstanding Balances",
               hole=0.45,
               color_discrete_sequence=px.colors.qualitative.Set2,
               height=520
            )
            fig_pie.update_traces(
                textposition='inside',
        textinfo='percent+label',
        insidetextorientation='radial',
        hovertemplate='<b>%{label}</b><br>Balance: NGN %{value:,.0f}<br>Share: %{percent:.1%}<extra></extra>'
            )
            fig_pie.update_layout(
                legend=dict(
            orientation="h",
            yanchor="bottom",
            y=-0.15,
            xanchor="center",
            x=0.5
        ),
        font=dict(family="Segoe UI, Arial", size=13),
        title_font=dict(size=22),
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)'
    )
            st.plotly_chart(fig_pie, use_container_width=True)

    else:
        st.info("No loan records yet. Add some loans to unlock beautiful visualizations!")


elif st.session_state.page == "View Records":
    if not df.empty:
        search_name = st.text_input("Search by Client Name (optional)", "")
        filtered = df if not search_name.strip() else df[df['names'].str.contains(search_name.strip(), case=False, na=False)]

        st.dataframe(
            filtered.drop(columns=['id'] if 'id' in filtered.columns else []),
            use_container_width=True,
            hide_index=True,
            column_config={
                "date": st.column_config.DateColumn("Date", disabled=True, format="YYYY-MM-DD"),
                "sn": st.column_config.NumberColumn(disabled=True),
                "interest": st.column_config.NumberColumn(disabled=True),
                "penalty_charged": st.column_config.NumberColumn(disabled=True),
                "total": st.column_config.NumberColumn(disabled=True),
                "g_total": st.column_config.NumberColumn(disabled=True),
                "balance": st.column_config.NumberColumn(disabled=True),
            }
        )
    else:
        st.info("No records.")


elif st.session_state.page == "Reports":
    if st.session_state.general_auth:
        tab1, tab2 = st.tabs(["Client Loan Report", "Profit & Loss Statement"])

        with tab1:
            if not df.empty:
                st.subheader("Client Loan Report")

                client_names = ["All Clients"] + sorted(df['names'].unique().tolist())
                selected_client = st.selectbox("Select Client", client_names, key="client_select")

                if selected_client == "All Clients":
                    filtered = df
                    fname_suffix = "all_clients"
                else:
                    filtered = df[df['names'] == selected_client]
                    fname_suffix = selected_client.replace(" ", "_").lower()

                if filtered.empty:
                    st.warning("No record found.")
                else:
                    c1, c2 = st.columns(2)
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    fname = f"loan_report_{fname_suffix}_{today_str}"

                    with c1:
                        csv_data = filtered.drop(columns=['id'] if 'id' in filtered else []).to_csv(index=False).encode('utf-8')
                        st.download_button("Download CSV", csv_data, f"{fname}.csv", "text/csv")

                    with c2:
                        pdf_buf = generate_fancy_pdf_single_client(filtered) if selected_client != "All Clients" else BytesIO()
                        if selected_client == "All Clients":
                            st.info("PDF export for all clients not implemented in this version.")
                        else:
                            st.download_button("Download PDF", pdf_buf, f"{fname}.pdf", "application/pdf")

                    st.markdown("**Preview:**")
                    st.dataframe(
                        filtered.drop(columns=['id'] if 'id' in filtered else []),
                        use_container_width=True
                    )
            else:
                st.info("No data available.")

        with tab2:
            st.subheader("Profit & Loss Statement")

            period_options = ["All Time", "This Year", "Last 12 Months", "Custom Range"]
            period = st.selectbox("Select Reporting Period", period_options)

            start_date = None
            end_date = datetime.now()

            if period == "This Year":
                start_date = datetime(end_date.year, 1, 1)
            elif period == "Last 12 Months":
                start_date = end_date - timedelta(days=365)
            elif period == "Custom Range":
                col1, col2 = st.columns(2)
                start_date = col1.date_input("From Date", value=end_date - timedelta(days=365))
                end_date = col2.date_input("To Date", value=end_date)

            if start_date is not None:
                start_date = pd.to_datetime(start_date)
                end_date = pd.to_datetime(end_date)
                loans_filtered = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
                expenses_filtered = load_expenses_df()
                expenses_filtered = expenses_filtered[(expenses_filtered['date'] >= start_date) & (expenses_filtered['date'] <= end_date)]
                income_filtered = load_other_income_df()
                income_filtered = income_filtered[(income_filtered['date'] >= start_date) & (income_filtered['date'] <= end_date)]
            else:
                loans_filtered = df
                expenses_filtered = load_expenses_df()
                income_filtered = load_other_income_df()

            revenue = {
                'interest': loans_filtered['interest'].sum(),
                'admin_fees': loans_filtered['admin_fees'].sum(),
                'penalty': loans_filtered['penalty_charged'].sum(),
            }

            other_income_grouped = income_filtered.groupby('category')['amount'].sum().to_dict()
            total_other_income = sum(other_income_grouped.values())

            total_revenue = revenue['interest'] + revenue['admin_fees'] + revenue['penalty'] + total_other_income

            expenses_grouped = expenses_filtered[expenses_filtered['category'] != "Owner's Equity Contribution"].groupby('category')['amount'].sum().to_dict()
            total_expenses = sum(expenses_grouped.values())

            equity_df = expenses_filtered[expenses_filtered['category'] == "Owner's Equity Contribution"]
            equity_contribution = equity_df['amount'].sum() if not equity_df.empty else 0.0

            net_operating = total_revenue - total_expenses
            final_position = net_operating + equity_contribution

            st.markdown("### Financial Summary")
            cols = st.columns(4)
            cols[0].metric("Total Revenue", f"NGN {total_revenue:,.2f}")
            cols[1].metric("Total Expenses", f"NGN {total_expenses:,.2f}")
            cols[2].metric("Net Operating Profit", f"NGN {net_operating:,.2f}", delta_color="normal")
            cols[3].metric("Capital Introduced", f"NGN {equity_contribution:,.2f}")

            st.markdown(f"**Final Net Position: NGN {final_position:,.2f}**")

            # ── Add New Income Source (clears after save) ──────────────────────────
            with st.expander("Add New Income Source"):
                if 'income_submitted' not in st.session_state:
                    st.session_state.income_submitted = False

                with st.form("add_other_income"):
                    inc_category = st.selectbox("Income Category", [
                        "Other Income", "Loan Recovery Fees", "Consultancy Income",
                        "Investment Income", "Custom"
                    ], key="inc_cat")
                    custom_cat = ""
                    if inc_category == "Custom":
                        custom_cat = st.text_input("Custom Income Category Name", key="inc_custom")

                    inc_amount = st.number_input("Amount (NGN)", min_value=0.0, step=1000.0, format="%.2f", key="inc_amt")
                    inc_date = st.date_input("Date", datetime.today(), key="inc_date")
                    inc_desc = st.text_input("Description (optional)", key="inc_desc")

                    if st.form_submit_button("Record Income"):
                        final_cat = custom_cat if inc_category == "Custom" else inc_category
                        if final_cat and inc_amount > 0:
                            save_other_income(final_cat, inc_amount, inc_date.strftime("%Y-%m-%d"), inc_desc)
                            st.success("Income recorded successfully!")
                            st.session_state.income_submitted = True
                            st.rerun()
                        else:
                            st.error("Category and amount are required")

                if st.session_state.income_submitted:
                    st.session_state.income_submitted = False
                    for k in ["inc_cat", "inc_custom", "inc_amt", "inc_date", "inc_desc"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.rerun()

            # ── Add Expense or Capital Contribution (clears after save) ───────────
            with st.expander("Add Expense or Capital Contribution"):
                if 'expense_submitted' not in st.session_state:
                    st.session_state.expense_submitted = False

                with st.form("add_expense"):
                    exp_category = st.selectbox("Category", [
                        "Allowances", "Social Support", "Administrative Expenses",
                        "Owner's Equity Contribution", "Other"
                    ], key="exp_cat")
                    custom_exp = ""
                    if exp_category == "Other":
                        custom_exp = st.text_input("Custom Expense Category Name", key="exp_custom")

                    exp_amount = st.number_input("Amount (NGN)", min_value=-1000000000.0, max_value=1000000000.0, step=1000.0, format="%.2f", key="exp_amt")
                    exp_date = st.date_input("Date", datetime.today(), key="exp_date")
                    exp_desc = st.text_input("Description (optional)", key="exp_desc")

                    if st.form_submit_button("Record"):
                        final_exp = custom_exp if exp_category == "Other" else exp_category
                        if final_exp:
                            save_expense(final_exp, exp_amount, exp_date.strftime("%Y-%m-%d"), exp_desc)
                            st.success("Recorded successfully!")
                            st.session_state.expense_submitted = True
                            st.rerun()
                        else:
                            st.error("Category is required")

                if st.session_state.expense_submitted:
                    st.session_state.expense_submitted = False
                    for k in ["exp_cat", "exp_custom", "exp_amt", "exp_date", "exp_desc"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.rerun()

            # ── P&L Download & Preview ─────────────────────────────────────────────
            pl_data = {
                'interest': revenue['interest'],
                'admin_fees': revenue['admin_fees'],
                'penalty': revenue['penalty'],
                'other_income': other_income_grouped,
                'total_revenue': total_revenue,
                'expenses': expenses_grouped,
                'total_expenses': total_expenses,
                'equity_contribution': equity_contribution,
                'net_operating_profit': net_operating,
                'final_position': final_position
            }

            c1, c2 = st.columns(2)
            today_str = datetime.now().strftime("%Y-%m-%d")
            fname_pl = f"profit_loss_{period.lower().replace(' ','_')}_{today_str}"

            with c1:
                pl_flat = {
                    'Period': period,
                    'Total Revenue': total_revenue,
                    'Total Expenses': total_expenses,
                    'Net Operating Profit': net_operating,
                    'Owner Equity Contribution': equity_contribution,
                    'Final Net Position': final_position
                }
                pl_flat.update({f"Income - {k}": v for k,v in other_income_grouped.items()})
                pl_flat.update({f"Expense - {k}": v for k,v in expenses_grouped.items()})
                pl_df = pd.DataFrame([pl_flat])
                csv_pl = pl_df.to_csv(index=False).encode('utf-8')
                st.download_button("Download CSV", csv_pl, f"{fname_pl}.csv", "text/csv")

            with c2:
                pdf_pl = generate_profit_loss_pdf(pl_data, period)
                st.download_button("Download PDF", pdf_pl, f"{fname_pl}.pdf", "application/pdf")

            st.markdown("**Transactions Preview**")
            st.dataframe(
                loans_filtered.drop(columns=['id'] if 'id' in loans_filtered else []),
                use_container_width=True
            )

            col_exp1, col_exp2 = st.columns(2)
            with col_exp1:
                if not expenses_filtered.empty:
                    st.markdown("**Expense Details**")
                    st.dataframe(expenses_filtered.style.format(precision=2, thousands=","), use_container_width=True)
            with col_exp2:
                if not income_filtered.empty:
                    st.markdown("**Other Income Details**")
                    st.dataframe(income_filtered.style.format(precision=2, thousands=","), use_container_width=True)
    else:
        general_login_form()


elif st.session_state.page == "Add Loan":
    if st.session_state.add_loan_auth:
        st.header("New Loan")

        # Flag for clearing
        if 'add_loan_submitted' not in st.session_state:
            st.session_state.add_loan_submitted = False

        with st.form("new_loan"):
            col1, col2 = st.columns(2)
            with col1:
                name = st.text_input("Client Name *", key="add_name")
                disp_date = st.date_input("Disbursement Date", datetime.today(), key="add_date")
                principal = st.number_input("Principal (NGN)", min_value=1000.0, step=5000.0, format="%.0f", key="add_principal")
            with col2:
                rate_pct = st.number_input("Monthly Interest %", min_value=0.0, step=0.5, value=5.0, key="add_rate")
                months = st.number_input("Duration (months)", min_value=1, step=1, value=3, key="add_months")
                admin_fee = st.number_input("Admin Fee (NGN)", min_value=0.0, step=1000.0, format="%.0f", key="add_admin")
                already_paid = st.number_input("Already Paid (NGN)", min_value=0.0, step=5000.0, format="%.0f", key="add_paid")

            submitted = st.form_submit_button("Save Loan", type="primary", use_container_width=True)

            if submitted:
                if name.strip() and principal > 0:
                    i_val, p_val, t_val, gt_val, b_val = calculate_loan_fields(
                        principal, rate_pct, months, admin_fee, already_paid, disp_date
                    )
                    sn_next = int(df['sn'].max() if not df.empty else 0) + 1
                    new_row = pd.DataFrame([{
                        'sn': sn_next,
                        'names': name.strip(),
                        'date': pd.to_datetime(disp_date),
                        'amount': principal,
                        'int_rate': rate_pct,
                        'duration': months,
                        'admin_fees': admin_fee,
                        'interest': i_val,
                        'penalty_charged': p_val,
                        'total': t_val,
                        'g_total': gt_val,
                        'amt_remitted': already_paid,
                        'balance': b_val
                    }])

                    if df.empty:
                        st.session_state.df = new_row
                    else:
                        st.session_state.df = pd.concat([df, new_row], ignore_index=True)

                    save_loans_df(st.session_state.df)
                    st.success("Loan saved successfully!")
                    st.session_state.add_loan_submitted = True
                    st.rerun()
                else:
                    st.error("Client name and principal amount are required")

        # Clear form after successful save
        if st.session_state.add_loan_submitted:
            st.session_state.add_loan_submitted = False
            for key in ["add_name", "add_date", "add_principal", "add_rate", "add_months", "add_admin", "add_paid"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

    else:
        section_login_form("Add Loan", 'add_loan_auth')


elif st.session_state.page == "Edit Loans":
    if st.session_state.edit_loan_auth:
        st.header("Edit Loan Record")

        if df.empty:
            st.info("No loans exist yet. Please add a loan first.")
        else:
            client_list = ["--- Select client ---"] + sorted(df['names'].unique().tolist())
            selected_name = st.selectbox("Client Name", client_list)

            if selected_name != "--- Select client ---":
                loan = df[df['names'] == selected_name].iloc[0].copy()

                with st.form("edit_single_loan"):
                    st.subheader(f"Editing: {selected_name}")

                    col1, col2 = st.columns(2)

                    with col1:
                        edit_name = st.text_input("Client Name", value=loan['names'])
                        edit_date = st.date_input(
                            "Disbursement Date",
                            value=loan['date'].date() if pd.notnull(loan['date']) else datetime.today().date()
                        )
                        edit_amount = st.number_input("Principal (NGN)", value=float(loan['amount']), min_value=0.0, step=1000.0)
                        edit_rate = st.number_input("Interest Rate %", value=float(loan['int_rate']), min_value=0.0, step=0.5)
                        edit_duration = st.number_input("Duration (months)", value=int(loan['duration']), min_value=1, step=1)

                    with col2:
                        edit_admin_fee = st.number_input("Admin Fee (NGN)", value=float(loan['admin_fees']), min_value=0.0, step=500.0)
                        edit_remitted = st.number_input("Amount Already Paid (NGN)", value=float(loan['amt_remitted']), min_value=0.0, step=1000.0)
                        edit_penalty = st.number_input(
                            "Penalty Charged (manual entry)",
                            value=float(loan['penalty_charged']),
                            min_value=0.0,
                            step=100.0,
                            format="%.2f",
                            help="If > 0: balance → 0, penalty added to total earned but NOT to interest column"
                        )

                        pure_interest = edit_amount * (edit_rate / 100) * edit_duration

                        if edit_penalty > 0:
                            total_add = edit_admin_fee + pure_interest + edit_penalty
                            g_total = edit_amount + total_add
                            final_balance = 0.0
                            note = "(Manual penalty → balance forced to 0)"
                        else:
                            auto_interest, auto_penalty, auto_total_add, auto_g_total, auto_balance = calculate_loan_fields(
                                edit_amount, edit_rate, edit_duration, edit_admin_fee, edit_remitted, edit_date
                            )
                            total_add = auto_total_add
                            g_total = auto_g_total
                            final_balance = auto_balance
                            note = "(Normal / automatic calculation)"

                        st.markdown(f"### Preview {note}")
                        st.metric("Interest (pure – saved in DB)", f"NGN {pure_interest:,.2f}")
                        st.metric("Penalty (entered)", f"NGN {edit_penalty:,.2f}")
                        st.metric("Total Add-ons (admin + interest + penalty)", f"NGN {total_add:,.2f}")
                        st.metric("Grand Total Due", f"NGN {g_total:,.2f}")
                        st.metric("Outstanding Balance", f"NGN {final_balance:,.2f}", delta_color="inverse")

                    submitted = st.form_submit_button("💾 Save Changes", type="primary", use_container_width=True)

                    if submitted:
                        pure_interest = edit_amount * (edit_rate / 100) * edit_duration

                        if edit_penalty > 0:
                            final_penalty = edit_penalty
                            total_add = edit_admin_fee + pure_interest + final_penalty
                            g_total = edit_amount + total_add
                            balance = 0.0
                        else:
                            _, final_penalty, total_add, g_total, balance = calculate_loan_fields(
                                edit_amount, edit_rate, edit_duration, edit_admin_fee, edit_remitted, edit_date
                            )

                        mask = df['names'] == selected_name
                        df.loc[mask, 'names']           = edit_name
                        df.loc[mask, 'date']            = pd.to_datetime(edit_date)
                        df.loc[mask, 'amount']          = edit_amount
                        df.loc[mask, 'int_rate']        = edit_rate
                        df.loc[mask, 'duration']        = edit_duration
                        df.loc[mask, 'admin_fees']      = edit_admin_fee
                        df.loc[mask, 'amt_remitted']    = edit_remitted
                        df.loc[mask, 'interest']        = round(pure_interest, 2)
                        df.loc[mask, 'penalty_charged'] = round(final_penalty, 2)
                        df.loc[mask, 'total']           = round(total_add, 2)
                        df.loc[mask, 'g_total']         = round(g_total, 2)
                        df.loc[mask, 'balance']         = round(max(balance, 0), 2)

                        st.session_state.df = df
                        save_loans_df(df)

                        st.success(f"Loan record for **{edit_name}** updated successfully!")
                        st.rerun()

    else:
        section_login_form("Edit Loans", 'edit_loan_auth')


elif st.session_state.page == "Admin Controls":
    if st.session_state.admin_auth:
        st.header("Admin Controls")
        st.subheader("Manage Users")

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Create New Admin User")
            with st.form("new_user"):
                new_username = st.text_input("New Username")
                new_password = st.text_input("New Password", type="password")
                submitted = st.form_submit_button("Create User")

                if submitted:
                    if new_username.strip() and new_password.strip():
                        if add_new_user(new_username.strip(), new_password.strip()):
                            st.success(f"User '{new_username}' created successfully")
                        else:
                            st.error("Username already exists")
                    else:
                        st.error("Both fields required")

        with col2:
            st.subheader("Existing Users")
            users = load_users_df()
            if not users.empty:
                for _, user in users.iterrows():
                    col_a, col_b = st.columns([4, 1])
                    col_a.write(f"**{user['username']}** (created {user['created_at'][:10]})")
                    if user['username'] != st.session_state.get('current_user') and st.button("Delete", key=f"del_{user['id']}"):
                        delete_user(user['id'], st.session_state.current_user)
                        st.success(f"User {user['username']} deleted")
                        st.rerun()
            else:
                st.info("No additional users yet (only default admin)")

        st.markdown("---")
        st.warning("Irreversible action")
        if st.button("RESET ALL LOAN DATA", type="primary"):
            cols = ['id','sn','names','date','amount','int_rate','duration','admin_fees','interest','penalty_charged','total','g_total','amt_remitted','balance']
            st.session_state.df = pd.DataFrame(columns=cols)
            save_loans_df(st.session_state.df)
            st.success("Loan database cleared")
            st.rerun()
    else:
        section_login_form("Admin Controls", 'admin_auth')


# Footer
st.markdown("---")
st.caption("Loan Management • Section-Level Authentication • Full P&L with Dynamic Income & Expenses")
