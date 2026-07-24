"""
MetaAds Pro - Campaign Creation Wizard
----------------------------------------
Replika UI pembuatan campaign (mirip screenshot) dibangun dengan Streamlit,
terhubung ke Meta Marketing API lewat SDK resmi `facebook-business`.

Semua value yang dikirim ke Meta (objective, status, billing_event,
optimization_goal, bid_strategy, dst) memakai ENUM dari SDK -> tidak ada
string mentah yang rawan typo.

Install dependencies:
    pip install streamlit facebook-business pillow opencv-python-headless

Run:
    streamlit run metaads_pro_app.py
"""

import base64
import io
import tempfile
import datetime as dt

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

try:
    import cv2  # opsional, untuk cek durasi/resolusi video sebelum upload
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

# ---------------------------------------------------------------------------
# SDK imports (enum-enum resmi dari Meta Marketing API Python SDK)
# ---------------------------------------------------------------------------
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.ad import Ad
from facebook_business.adobjects.adcreative import AdCreative
from facebook_business.adobjects.adimage import AdImage
from facebook_business.adobjects.advideo import AdVideo
from facebook_business.adobjects.adpreview import AdPreview
from facebook_business.adobjects.user import User
from facebook_business.exceptions import FacebookRequestError

# ---------------------------------------------------------------------------
# Page config + sedikit styling supaya nuansanya dekat dengan mockup
# ---------------------------------------------------------------------------
st.set_page_config(page_title="MetaAds Pro", layout="wide", page_icon="📣")

st.markdown(
    """
    <style>
    .stApp { background-color: #f4f6f9; }
    div[data-testid="stMetricValue"] { font-size: 1.1rem; }
    .step-badge {
        display:inline-block; width:26px; height:26px; border-radius:50%;
        background:#1877F2; color:white; text-align:center; line-height:26px;
        font-weight:600; margin-right:8px;
    }
    .section-title { font-size:1.2rem; font-weight:700; margin-bottom:0.2rem; }
    .card {
        background:white; border-radius:10px; padding:1.2rem 1.4rem;
        border:1px solid #e5e7eb; margin-bottom:1rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Mapping label UI -> ENUM SDK (bukan string mentah)
# ---------------------------------------------------------------------------
OBJECTIVE_MAP = {
    "Awareness": Campaign.Objective.outcome_awareness,
    "Traffic": Campaign.Objective.outcome_traffic,
    "Interaction": Campaign.Objective.outcome_engagement,
    "Prospects": Campaign.Objective.outcome_leads,
    "App Promotion": Campaign.Objective.outcome_app_promotion,
    "Sale": Campaign.Objective.outcome_sales,
}

SPECIAL_AD_CATEGORY_MAP = {
    "No category": Campaign.SpecialAdCategory.none,
    "Employment": Campaign.SpecialAdCategory.employment,
    "Housing": Campaign.SpecialAdCategory.housing,
    "Credit": Campaign.SpecialAdCategory.credit,
    ""POLITICAL_AND_ISSUE_ADS": Campaign.SpecialAdCategory.issues_election_politics,
}

OPTIMIZATION_GOAL_MAP = {
    "Conversions": AdSet.OptimizationGoal.offsite_conversions,
    "Link Clicks": AdSet.OptimizationGoal.link_clicks,
    "Impressions": AdSet.OptimizationGoal.impressions,
    "Reach": AdSet.OptimizationGoal.reach,
    "Landing Page Views": AdSet.OptimizationGoal.landing_page_views,
}

BILLING_EVENT_MAP = {
    "Impressions": AdSet.BillingEvent.impressions,
    "Link Clicks": AdSet.BillingEvent.link_clicks,
}

BID_STRATEGY_MAP = {
    "Lowest cost (tanpa cap)": AdSet.BidStrategy.lowest_cost_without_cap,
    "Lowest cost with bid cap": AdSet.BidStrategy.lowest_cost_with_bid_cap,
    "Cost cap": AdSet.BidStrategy.cost_cap,
}

AD_FORMAT_OPTIONS = ["Single Image", "Single Video", "Carousel", "Collection"]

# Semua status pakai enum, default selalu PAUSED demi keamanan
STATUS_DRAFT = Campaign.Status.paused
ADSET_STATUS_DRAFT = AdSet.Status.paused
AD_STATUS_DRAFT = Ad.Status.paused

# ---------------------------------------------------------------------------
# Helper: ambil daftar Facebook Page + Instagram Profile yang terhubung
# ---------------------------------------------------------------------------
def fetch_connected_pages(token: str):
    """Mengambil semua Page yang dikelola user, beserta Instagram Business
    Account yang terhubung ke tiap Page (kalau ada). Return list of dict:
    [{"page_id": ..., "page_name": ..., "ig_id": ..., "ig_username": ...}, ...]
    """
    FacebookAdsApi.init(access_token=token)
    me = User(fbid="me")
    pages = me.get_accounts(fields=[
        "id", "name", "instagram_business_account{id,username}",
    ])
    result = []
    for p in pages:
        ig = p.get("instagram_business_account")
        result.append({
            "page_id": p.get("id"),
            "page_name": p.get("name"),
            "ig_id": ig.get("id") if ig else None,
            "ig_username": ig.get("username") if ig else None,
        })
    return result


# ---------------------------------------------------------------------------
# Helper: upload gambar ke Meta -> dapat image_hash (untuk creative)
# ---------------------------------------------------------------------------
def upload_ad_image(ad_account_id: str, access_token: str, image_bytes: bytes) -> str:
    """Upload gambar (bytes) ke Meta lewat AdImage SDK, return image_hash."""
    FacebookAdsApi.init(access_token=access_token)
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    image = AdImage(parent_id=ad_account_id)
    image[AdImage.Field.bytes] = encoded
    image.remote_create()
    return image[AdImage.Field.hash]


# Rasio gambar yang direkomendasikan Meta per jenis placement (width/height)
RECOMMENDED_RATIOS = {
    "1:1 (Feed persegi)": 1.0,
    "4:5 (Feed vertikal)": 0.8,
    "1.91:1 (Landscape / Link Ads)": 1.91,
    "9:16 (Story / Reels)": 0.5625,
}
RATIO_TOLERANCE = 0.05  # toleransi 5% sebelum dianggap "tidak cocok"


def check_image_ratio(image_bytes: bytes):
    """Cek dimensi & rasio gambar, bandingkan dengan rasio rekomendasi Meta.
    Return (width, height, closest_label, closest_ratio, is_close_enough)."""
    img = Image.open(io.BytesIO(image_bytes))
    width, height = img.size
    actual_ratio = width / height

    closest_label, closest_ratio = min(
        RECOMMENDED_RATIOS.items(), key=lambda kv: abs(kv[1] - actual_ratio)
    )
    is_close_enough = abs(closest_ratio - actual_ratio) / closest_ratio <= RATIO_TOLERANCE
    return width, height, closest_label, closest_ratio, is_close_enough


def upload_ad_video(ad_account_id: str, access_token: str, video_bytes: bytes, suffix: str = ".mp4") -> str:
    """Upload video ke Meta lewat AdVideo SDK, return video_id."""
    FacebookAdsApi.init(access_token=access_token)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name
    video = AdVideo(parent_id=ad_account_id)
    video[AdVideo.Field.filepath] = tmp_path
    video.remote_create()
    return video[AdVideo.Field.id]


# Batas spesifikasi video menurut rekomendasi Meta untuk Feed Ads
MAX_VIDEO_SIZE_MB = 4096  # 4GB
MIN_VIDEO_DURATION_SEC = 1
MAX_VIDEO_DURATION_SEC = 241 * 60  # 241 menit


def check_video_specs(video_bytes: bytes, suffix: str = ".mp4"):
    """Cek ukuran file, durasi, dan resolusi video sebelum upload.
    Return dict berisi size_mb, duration_sec, width, height, warnings (list)."""
    size_mb = len(video_bytes) / (1024 * 1024)
    warnings = []
    if size_mb > MAX_VIDEO_SIZE_MB:
        warnings.append(f"Ukuran file {size_mb:.0f}MB melebihi batas {MAX_VIDEO_SIZE_MB}MB.")

    duration_sec, width, height = None, None, None
    if HAS_CV2:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name
        cap = cv2.VideoCapture(tmp_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        cap.release()
        if fps > 0:
            duration_sec = frame_count / fps
            if duration_sec < MIN_VIDEO_DURATION_SEC:
                warnings.append(f"Durasi {duration_sec:.1f}s lebih pendek dari minimum {MIN_VIDEO_DURATION_SEC}s.")
            if duration_sec > MAX_VIDEO_DURATION_SEC:
                warnings.append(f"Durasi {duration_sec/60:.0f} menit melebihi maksimum 241 menit.")
        if width and height and (width < 1080 or height < 1080):
            warnings.append(f"Resolusi {width}×{height}px di bawah rekomendasi minimum 1080×1080px.")
    else:
        warnings.append("Install `opencv-python-headless` untuk validasi durasi & resolusi video otomatis.")

    return {
        "size_mb": size_mb, "duration_sec": duration_sec,
        "width": width, "height": height, "warnings": warnings,
    }


# Mapping label UI -> ENUM SDK untuk format preview (AdPreview.AdFormat).
# Dibungkus getattr supaya tidak crash kalau versi SDK kamu tidak punya
# member enum tertentu (nama member bisa sedikit beda antar versi SDK).
def _safe_enum(enum_cls, name, fallback):
    return getattr(enum_cls, name, fallback)


AD_PREVIEW_FORMAT_MAP = {
    "Facebook Feed (Mobile)": _safe_enum(AdPreview.AdFormat, "mobile_feed_standard", "MOBILE_FEED_STANDARD"),
    "Facebook Feed (Desktop)": _safe_enum(AdPreview.AdFormat, "desktop_feed_standard", "DESKTOP_FEED_STANDARD"),
    "Instagram Feed": _safe_enum(AdPreview.AdFormat, "instagram_standard", "INSTAGRAM_STANDARD"),
    "Instagram Stories": _safe_enum(AdPreview.AdFormat, "instagram_story", "INSTAGRAM_STORY"),
    "Facebook Stories": _safe_enum(AdPreview.AdFormat, "facebook_story_mobile", "FACEBOOK_STORY_MOBILE"),
    "Marketplace": _safe_enum(AdPreview.AdFormat, "marketplace_mobile", "MARKETPLACE_MOBILE"),
}


def build_object_story_spec():
    """Bangun object_story_spec dari session_state saat ini (dipakai bareng
    oleh publish_campaign_to_meta() dan fetch_ad_preview_html() supaya konsisten)."""
    creative_content = st.session_state.get("creative_content", {})
    identity = st.session_state.get("identity", {})
    ad_format = st.session_state.get("ad_format", "Single Image")
    page_id = identity.get("page_id") or "<FB_PAGE_ID>"

    if ad_format == "Carousel":
        cards = st.session_state.get("carousel_cards", [])
        child_attachments = [
            {"link": c["link"], "name": c["name"],
             "description": c["description"], "image_hash": c["image_hash"]}
            for c in cards
        ]
        link_data = {
            "link": creative_content.get("link_url", "https://example.com"),
            "message": creative_content.get("primary_text", ""),
            "child_attachments": child_attachments,
            "multi_share_optimized": True,
        }
        return {"page_id": page_id, "instagram_actor_id": identity.get("instagram_id"),
                "link_data": link_data}

    if ad_format == "Single Video":
        video_data = {
            "video_id": creative_content.get("video_id"),
            "title": creative_content.get("headline", ""),
            "message": creative_content.get("primary_text", ""),
            "call_to_action": {
                "type": "LEARN_MORE",
                "value": {"link": creative_content.get("link_url", "https://example.com")},
            },
        }
        if creative_content.get("thumbnail_hash"):
            video_data["image_hash"] = creative_content["thumbnail_hash"]
        return {"page_id": page_id, "instagram_actor_id": identity.get("instagram_id"),
                "video_data": video_data}

    link_data = {
        "link": creative_content.get("link_url", "https://example.com"),
        "message": creative_content.get("primary_text", ""),
        "name": creative_content.get("headline", ""),
    }
    if creative_content.get("image_hash"):
        link_data["image_hash"] = creative_content["image_hash"]
    return {"page_id": page_id, "instagram_actor_id": identity.get("instagram_id"),
            "link_data": link_data}


def fetch_ad_preview_html(ad_account_id: str, access_token: str, preview_format_enum: str) -> str:
    """Panggil Meta Ad Preview API (/generatepreviews) dan return HTML iframe asli."""
    FacebookAdsApi.init(access_token=access_token)
    account = AdAccount(ad_account_id)
    object_story_spec = build_object_story_spec()
    params = {
        "creative": {"object_story_spec": object_story_spec},
        "ad_format": preview_format_enum,
    }
    previews = account.get_generate_previews(params=params)
    for p in previews:
        return p.get(AdPreview.Field.body)
    return ""


def render_image_uploader(key: str, ad_account_id: str, access_token: str) -> str:
    """Widget upload gambar + validasi rasio + tombol kirim ke Meta. Return
    image_hash terkini (otomatis terisi setelah upload, atau bisa ditimpa manual)."""
    hash_state_key = f"hash_{key}"

    uploaded_file = st.file_uploader(
        "Upload gambar (JPG/PNG)", type=["png", "jpg", "jpeg"], key=f"upload_{key}"
    )
    if uploaded_file is not None:
        file_bytes = uploaded_file.getvalue()
        st.image(file_bytes, width=160)

        try:
            width, height, closest_label, closest_ratio, is_ok = check_image_ratio(file_bytes)
            actual_ratio = width / height
            if is_ok:
                st.success(f"{width}×{height}px — rasio ~{actual_ratio:.2f} cocok dengan {closest_label}.")
            else:
                st.warning(
                    f"{width}×{height}px — rasio {actual_ratio:.2f} agak jauh dari rekomendasi "
                    f"Meta terdekat ({closest_label} = {closest_ratio:.2f}). Gambar tetap bisa "
                    f"diupload, tapi mungkin di-crop otomatis oleh Meta saat ditampilkan."
                )
        except Exception:  # noqa: BLE001
            st.caption("Tidak bisa membaca dimensi gambar untuk validasi rasio.")

        if st.button("⬆️ Upload ke Meta (dapat image_hash)", key=f"btn_upload_{key}"):
            if not (access_token and ad_account_id):
                st.error("Isi Access Token & Ad Account ID di sidebar dulu.")
            else:
                try:
                    img_hash = upload_ad_image(
                        ad_account_id, access_token, file_bytes
                    )
                    st.session_state[hash_state_key] = img_hash
                    st.success(f"Berhasil upload. image_hash: {img_hash}")
                except FacebookRequestError as e:
                    st.error(f"Meta API error: {e.api_error_message()}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Gagal upload: {e}")

    current_hash = st.session_state.get(hash_state_key, "")
    manual_hash = st.text_input(
        "Image Hash", value=current_hash, key=f"hashfield_{key}",
        help="Terisi otomatis setelah upload berhasil, atau isi manual kalau sudah punya hash.",
    )
    st.session_state[hash_state_key] = manual_hash
    return manual_hash


def render_video_uploader(key: str, ad_account_id: str, access_token: str) -> str:
    """Widget upload video + tombol kirim ke Meta. Return video_id terkini."""
    video_id_state_key = f"video_id_{key}"

    uploaded_video = st.file_uploader(
        "Upload video (MP4)", type=["mp4", "mov"], key=f"upload_video_{key}"
    )
    if uploaded_video is not None:
        st.video(uploaded_video)
        suffix_check = ".mp4" if uploaded_video.name.lower().endswith(".mp4") else ".mov"
        specs = check_video_specs(uploaded_video.getvalue(), suffix_check)
        if specs["duration_sec"] is not None:
            st.caption(
                f"Durasi ~{specs['duration_sec']:.1f}s · "
                f"Resolusi {specs['width']}×{specs['height']}px · "
                f"Ukuran {specs['size_mb']:.1f}MB"
            )
        else:
            st.caption(f"Ukuran {specs['size_mb']:.1f}MB")
        for w in specs["warnings"]:
            st.warning(w)

        if st.button("⬆️ Upload Video ke Meta (dapat video_id)", key=f"btn_upload_video_{key}"):
            if not (access_token and ad_account_id):
                st.error("Isi Access Token & Ad Account ID di sidebar dulu.")
            else:
                try:
                    with st.spinner("Mengunggah video, proses ini bisa memakan waktu..."):
                        video_id = upload_ad_video(
                            ad_account_id, access_token, uploaded_video.getvalue(), suffix_check
                        )
                    st.session_state[video_id_state_key] = video_id
                    st.success(f"Berhasil upload. video_id: {video_id}")
                except FacebookRequestError as e:
                    st.error(f"Meta API error: {e.api_error_message()}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Gagal upload video: {e}")

    current_video_id = st.session_state.get(video_id_state_key, "")
    manual_video_id = st.text_input(
        "Video ID", value=current_video_id, key=f"videoidfield_{key}",
        help="Terisi otomatis setelah upload berhasil, atau isi manual kalau sudah punya video_id.",
    )
    st.session_state[video_id_state_key] = manual_video_id
    return manual_video_id


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "step" not in st.session_state:
    st.session_state.step = 1
if "connected_pages" not in st.session_state:
    st.session_state.connected_pages = []

STEPS = ["Campaign", "Ad Set", "Ads", "Review"]

# ---------------------------------------------------------------------------
# Sidebar: koneksi API + navigasi step
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🔵 MetaAds Pro")
    st.caption("Campaign builder terhubung ke Meta Marketing API")

    with st.expander("⚙️ Koneksi API", expanded=False):
        access_token = st.text_input("Access Token", type="password")
        app_id = st.text_input("App ID")
        app_secret = st.text_input("App Secret", type="password")
        ad_account_id = st.text_input("Ad Account ID", placeholder="act_1234567890")
        api_status = "🟢 Connected" if access_token and ad_account_id else "⚪ Belum terhubung"
        st.caption(f"API Status: {api_status}")

        if st.button("🔄 Ambil Daftar Page & Instagram"):
            if not access_token:
                st.error("Isi Access Token dulu.")
            else:
                try:
                    st.session_state.connected_pages = fetch_connected_pages(access_token)
                    st.success(f"{len(st.session_state.connected_pages)} Page ditemukan.")
                except FacebookRequestError as e:
                    st.error(f"Meta API error: {e.api_error_message()}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Gagal mengambil daftar Page: {e}")

    st.markdown("---")
    for i, label in enumerate(STEPS, start=1):
        prefix = "●" if st.session_state.step == i else "○"
        if st.button(f"{prefix} {i}. {label}", use_container_width=True, key=f"nav_{i}"):
            st.session_state.step = i

# ---------------------------------------------------------------------------
# Layout utama: form (kiri, lebar) + preview & estimasi (kanan)
# ---------------------------------------------------------------------------
col_main, col_side = st.columns([2.3, 1])

# =============================== STEP 1: CAMPAIGN ==========================
with col_main:
    if st.session_state.step == 1:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<span class="step-badge">1</span> **Campaign** — '
                    'Define your campaign objective and budget', unsafe_allow_html=True)
        st.divider()

        st.session_state.campaign_name = st.text_input(
            "Campaign Name", value=st.session_state.get("campaign_name", "Summer Sale 2026")
        )

        st.markdown("**Campaign Objective**")
        objective_label = st.radio(
            "Campaign Objective", list(OBJECTIVE_MAP.keys()),
            horizontal=True, label_visibility="collapsed",
            index=list(OBJECTIVE_MAP.keys()).index(
                st.session_state.get("objective_label", "Traffic")
            ),
        )
        st.session_state.objective_label = objective_label
        st.session_state.objective_enum = OBJECTIVE_MAP[objective_label]

        st.markdown("**Budget**")
        b1, b2, b3 = st.columns(3)
        with b1:
            budget_strategy = st.radio(
                "Budget Strategy", ["Campaign budget", "Ad set budget"], label_visibility="visible"
            )
            st.session_state.budget_strategy = budget_strategy
        with b2:
            daily_budget = st.number_input(
                "Daily Budget (IDR)", min_value=20000, step=10000,
                value=st.session_state.get("daily_budget", 100000),
            )
            st.session_state.daily_budget = daily_budget
        with b3:
            lifetime_budget = st.number_input(
                "Lifetime Budget (Optional, IDR)", min_value=0, step=50000,
                value=st.session_state.get("lifetime_budget", 0),
            )
            st.session_state.lifetime_budget = lifetime_budget

        s1, s2, s3 = st.columns(3)
        with s1:
            start_date = st.date_input("Start date", value=dt.date.today())
            st.session_state.start_date = start_date
        with s2:
            no_end_date = st.checkbox("No end date", value=True)
        with s3:
            end_date = None
            if not no_end_date:
                end_date = st.date_input("End date")
            st.session_state.end_date = end_date

        special_cat_label = st.selectbox(
            "Campaign Special Ad Category", list(SPECIAL_AD_CATEGORY_MAP.keys())
        )
        st.session_state.special_cat_enum = SPECIAL_AD_CATEGORY_MAP[special_cat_label]

        st.markdown('</div>', unsafe_allow_html=True)
        if st.button("Lanjut ke Ad Set ➜"):
            st.session_state.step = 2
            st.rerun()

    # =============================== STEP 2: AD SET =========================
    elif st.session_state.step == 2:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<span class="step-badge">2</span> **Ad Set** — '
                    'Define your audience, placement and budget', unsafe_allow_html=True)
        st.divider()

        tabs = st.tabs(["Audience", "Placements", "Budget & Schedule", "Optimization"])

        with tabs[0]:
            age_col1, age_col2 = st.columns(2)
            with age_col1:
                age_min = st.number_input("Age min", 13, 65, value=18)
            with age_col2:
                age_max = st.number_input("Age max", 13, 65, value=65)
            gender = st.radio("Gender", ["All", "Male", "Female"], horizontal=True)
            languages = st.multiselect("Languages", ["All languages", "Indonesian", "English"],
                                        default=["All languages"])
            detailed_targeting = st.multiselect(
                "Detailed Targeting",
                ["Parents", "Online shopping", "Fashion", "Baby", "Clothing", "Mother's Day"],
                default=["Parents", "Online shopping", "Fashion"],
            )
            audience_name = st.text_input("Audience Name", value="Indonesia Parents", max_chars=50)

            st.session_state.targeting = {
                "age_min": age_min,
                "age_max": age_max,
                "gender": gender,
                "languages": languages,
                "detailed_targeting": detailed_targeting,
                "audience_name": audience_name,
            }

        with tabs[1]:
            placement_mode = st.radio(
                "Placement", ["Automatic (Advantage+)", "Manual Placement"], horizontal=True
            )
            platforms = st.multiselect(
                "Platforms",
                ["Facebook", "Instagram", "Messenger", "Audience Network"],
                default=["Facebook", "Instagram"],
            )
            st.session_state.placements = {"mode": placement_mode, "platforms": platforms}

        with tabs[2]:
            optimization_goal_label = st.selectbox(
                "Optimization Goal", list(OPTIMIZATION_GOAL_MAP.keys())
            )
            billing_event_label = st.selectbox("Billing Event", list(BILLING_EVENT_MAP.keys()))
            bid_strategy_label = st.selectbox("Bid Strategy", list(BID_STRATEGY_MAP.keys()))

            st.session_state.optimization_goal_enum = OPTIMIZATION_GOAL_MAP[optimization_goal_label]
            st.session_state.billing_event_enum = BILLING_EVENT_MAP[billing_event_label]
            st.session_state.bid_strategy_enum = BID_STRATEGY_MAP[bid_strategy_label]

        with tabs[3]:
            st.caption("Semua optimizations diaktifkan (default Advantage+).")

        st.markdown('</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("⟲ Kembali ke Campaign"):
                st.session_state.step = 1
                st.rerun()
        with c2:
            if st.button("Lanjut ke Ads ➜"):
                st.session_state.step = 3
                st.rerun()

    # =============================== STEP 3: ADS =============================
    elif st.session_state.step == 3:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<span class="step-badge">3</span> **Ads** — '
                    'Create your ad creative and content', unsafe_allow_html=True)
        st.divider()

        ad_name = st.text_input("Ad Name", value="Summer Sale - Carousel 01", max_chars=100)
        st.session_state.ad_name = ad_name

        # --- Identity: Facebook Page & Instagram, otomatis dari fetch_connected_pages ---
        pages = st.session_state.get("connected_pages", [])
        i1, i2 = st.columns(2)
        if pages:
            page_labels = [p["page_name"] for p in pages]
            with i1:
                selected_page_label = st.selectbox("Facebook Page", page_labels)
            selected_page = next(p for p in pages if p["page_name"] == selected_page_label)
            with i2:
                ig_label = selected_page["ig_username"] or "(tidak ada Instagram terhubung)"
                st.selectbox("Instagram Profile", [ig_label], disabled=True)
            st.session_state.identity = {
                "page": selected_page["page_name"],
                "page_id": selected_page["page_id"],
                "instagram": selected_page["ig_username"],
                "instagram_id": selected_page["ig_id"],
            }
        else:
            st.caption("Belum ada daftar Page — klik '🔄 Ambil Daftar Page & Instagram' "
                       "di sidebar, atau isi manual di bawah.")
            with i1:
                facebook_page = st.text_input("Facebook Page (manual)", value="My Business")
            with i2:
                instagram_profile = st.text_input("Instagram Profile (manual)", value="my.business")
            st.session_state.identity = {
                "page": facebook_page, "page_id": None,
                "instagram": instagram_profile, "instagram_id": None,
            }

        ad_format = st.radio("Ad Setup - Format", AD_FORMAT_OPTIONS, horizontal=True)
        st.session_state.ad_format = ad_format

        primary_text = st.text_area("Primary Text", value="Diskon terbesar tahun ini! Dapatkan produk terbaik untuk keluarga kamu")
        headline = st.text_input("Headline", value="Summer Sale Up To 50% Off")
        link_url = st.text_input("Website URL", value="https://mybusiness.com")
        st.session_state.creative_content = {
            "primary_text": primary_text, "headline": headline, "link_url": link_url,
        }

        if ad_format == "Single Image":
            st.markdown("**Gambar Iklan**")
            single_image_hash = render_image_uploader("single_image", ad_account_id, access_token)
            st.session_state.creative_content["image_hash"] = single_image_hash

        if ad_format == "Single Video":
            st.markdown("**Video Iklan**")
            single_video_id = render_video_uploader("single_video", ad_account_id, access_token)
            st.session_state.creative_content["video_id"] = single_video_id
            st.markdown("**Thumbnail Video**")
            thumbnail_hash = render_image_uploader("single_video_thumb", ad_account_id, access_token)
            st.session_state.creative_content["thumbnail_hash"] = thumbnail_hash

        # --- Builder khusus Carousel: beberapa kartu (child_attachments) ---
        if ad_format == "Carousel":
            st.markdown("**Carousel Cards**")
            num_cards = st.number_input("Jumlah kartu", min_value=2, max_value=10,
                                         value=len(st.session_state.get("carousel_cards", [])) or 2)
            cards = []
            for idx in range(int(num_cards)):
                with st.expander(f"Kartu {idx + 1}", expanded=(idx < 2)):
                    c_link = st.text_input("Link", value=link_url, key=f"car_link_{idx}")
                    c_name = st.text_input("Judul kartu", value=f"Produk {idx + 1}", key=f"car_name_{idx}")
                    c_desc = st.text_input("Deskripsi", value="", key=f"car_desc_{idx}")
                    c_image_hash = render_image_uploader(
                        f"car_{idx}", ad_account_id, access_token
                    )
                    cards.append({
                        "link": c_link, "name": c_name,
                        "description": c_desc, "image_hash": c_image_hash,
                    })
            st.session_state.carousel_cards = cards

        st.markdown('</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("⟲ Kembali ke Ad Set"):
                st.session_state.step = 2
                st.rerun()
        with c2:
            if st.button("Lanjut ke Review ➜"):
                st.session_state.step = 4
                st.rerun()

    # =============================== STEP 4: REVIEW =========================
    elif st.session_state.step == 4:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<span class="step-badge">4</span> **Review** — Review & publish', unsafe_allow_html=True)
        st.divider()

        st.write("**Campaign:**", st.session_state.get("campaign_name"))
        st.write("**Objective (enum):**", st.session_state.get("objective_enum"))
        st.write("**Daily Budget:**", f"Rp {st.session_state.get('daily_budget', 0):,}")
        st.write("**Audience:**", st.session_state.get("targeting", {}).get("audience_name"))
        st.write("**Optimization Goal (enum):**", st.session_state.get("optimization_goal_enum"))
        st.write("**Bid Strategy (enum):**", st.session_state.get("bid_strategy_enum"))
        st.write("**Ad Format:**", st.session_state.get("ad_format"))

        st.markdown('</div>', unsafe_allow_html=True)
        if st.button("⟲ Kembali ke Ads"):
            st.session_state.step = 3
            st.rerun()

# ---------------------------------------------------------------------------
# Kolom kanan: preview sederhana + estimasi
# ---------------------------------------------------------------------------
with col_side:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("**Ad Preview**")

    preview_format_label = st.selectbox(
        "Placement Preview", list(AD_PREVIEW_FORMAT_MAP.keys()), key="preview_format_label"
    )

    if st.button("🔍 Ambil Preview Asli dari Meta"):
        if not (access_token and ad_account_id):
            st.error("Isi Access Token & Ad Account ID di sidebar dulu.")
        else:
            try:
                with st.spinner("Meminta preview ke Meta..."):
                    preview_html = fetch_ad_preview_html(
                        ad_account_id, access_token,
                        AD_PREVIEW_FORMAT_MAP[preview_format_label],
                    )
                st.session_state["last_preview_html"] = preview_html
            except FacebookRequestError as e:
                st.error(f"Meta API error: {e.api_error_message()}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Gagal mengambil preview: {e}")

    if st.session_state.get("last_preview_html"):
        # Preview asli dari Meta = iframe HTML resmi, dirender langsung
        components.html(st.session_state["last_preview_html"], height=500, scrolling=True)
    else:
        # Fallback: mock preview lokal selama belum generate dari Meta
        st.caption("Belum ada preview dari Meta — menampilkan mock lokal.")
        st.markdown(f"📄 **{st.session_state.get('identity', {}).get('page', 'My Business')}** · Sponsored")
        st.write(st.session_state.get("creative_content", {}).get(
            "primary_text", "Diskon terbesar tahun ini!"
        ))
        st.image(
            "https://via.placeholder.com/400x260.png?text=Creative+Preview",
            use_container_width=True,
        )
        st.write(f"**{st.session_state.get('creative_content', {}).get('headline', '')}**")
        st.caption(st.session_state.get("creative_content", {}).get("link_url", ""))
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("**Estimasi (indikatif)**")
    m1, m2 = st.columns(2)
    m1.metric("Estimated Reach", "18.000 - 23.000")
    m2.metric("Estimated Clicks", "1.200 - 1.800")
    m3, m4 = st.columns(2)
    m3.metric("CTR", "2.45%")
    m4.metric("Estimated CPM", "Rp 5.000 - 7.000")
    st.caption("Angka ini hanya estimasi lokal, bukan dari Meta Insights API.")
    st.markdown('</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Fungsi pemanggil Meta Marketing API (memakai enum, bukan string mentah)
# ---------------------------------------------------------------------------
def publish_campaign_to_meta():
    """Membuat Campaign -> Ad Set -> Creative -> Ad di Meta, semua status PAUSED."""
    if not (access_token and ad_account_id):
        st.error("Isi Access Token dan Ad Account ID di sidebar (⚙️ Koneksi API) dulu.")
        return

    try:
        FacebookAdsApi.init(access_token=access_token, app_id=app_id or None,
                             app_secret=app_secret or None)
        account = AdAccount(ad_account_id)

        campaign_params = {
            "name": st.session_state.get("campaign_name", "Untitled Campaign"),
            "objective": st.session_state.get("objective_enum", Campaign.Objective.outcome_traffic),
            "status": STATUS_DRAFT,  # enum, selalu PAUSED saat pertama dibuat
            "special_ad_categories": [st.session_state.get(
                "special_cat_enum", Campaign.SpecialAdCategory.none
            )] if st.session_state.get("special_cat_enum") != Campaign.SpecialAdCategory.none else [],
        }
        campaign = account.create_campaign(params=campaign_params)

        targeting = st.session_state.get("targeting", {})
        adset_params = {
            "name": f"{st.session_state.get('campaign_name', 'Campaign')} - Ad Set",
            "campaign_id": campaign["id"],
            "daily_budget": int(st.session_state.get("daily_budget", 100000)),
            "billing_event": st.session_state.get("billing_event_enum", AdSet.BillingEvent.impressions),
            "optimization_goal": st.session_state.get(
                "optimization_goal_enum", AdSet.OptimizationGoal.link_clicks
            ),
            "bid_strategy": st.session_state.get(
                "bid_strategy_enum", AdSet.BidStrategy.lowest_cost_without_cap
            ),
            "targeting": {
                "geo_locations": {"countries": ["ID"]},
                "age_min": targeting.get("age_min", 18),
                "age_max": targeting.get("age_max", 65),
            },
            "status": ADSET_STATUS_DRAFT,  # enum
        }
        adset = account.create_ad_set(params=adset_params)

        # Object story spec dibangun lewat fungsi bersama supaya konsisten
        # dengan yang dipakai di fitur "Generate Preview" (tidak duplikat logic)
        object_story_spec = build_object_story_spec()

        creative_params = {
            "name": f"{st.session_state.get('ad_name', 'Ad')} - Creative",
            "object_story_spec": object_story_spec,
        }
        creative = account.create_ad_creative(params=creative_params)

        ad_params = {
            "name": st.session_state.get("ad_name", "Ad"),
            "adset_id": adset["id"],
            "creative": {"creative_id": creative["id"]},
            "status": AD_STATUS_DRAFT,  # enum
        }
        ad = account.create_ad(params=ad_params)

        st.success(
            f"Berhasil dibuat (status PAUSED): Campaign {campaign['id']}, "
            f"Ad Set {adset['id']}, Ad {ad['id']}. Aktifkan manual setelah dicek."
        )
    except FacebookRequestError as e:
        st.error(f"Meta API error: {e.api_error_message()}")
    except Exception as e:  # noqa: BLE001
        st.error(f"Terjadi kesalahan: {e}")


# ---------------------------------------------------------------------------
# Bottom action bar
# ---------------------------------------------------------------------------
st.divider()
bcol1, bcol2, bcol3 = st.columns([1, 1, 2])
with bcol1:
    if st.button("💾 Save Draft"):
        st.info("Draft disimpan secara lokal di session (belum dikirim ke Meta).")
with bcol2:
    if st.button("📄 Duplicate"):
        st.info("Konfigurasi saat ini diduplikasi (implementasikan sesuai kebutuhanmu).")
with bcol3:
    if st.button("🚀 Publish Campaign", type="primary"):
        publish_campaign_to_meta()
