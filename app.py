import re
import time
import requests
import streamlit as st
from spotipy.oauth2 import SpotifyOAuth
import spotipy

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
st.set_page_config(page_title="YouTube -> Spotify Playlist Converter", page_icon="🎵")

YOUTUBE_API_KEY = st.secrets.get("YOUTUBE_API_KEY", "")
SPOTIFY_CLIENT_ID = st.secrets.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = st.secrets.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = st.secrets.get("SPOTIFY_REDIRECT_URI", "")

SPOTIFY_SCOPE = "playlist-modify-public playlist-modify-private"

# ---------------------------------------------------------------------------
# HELPERS: YouTube
# ---------------------------------------------------------------------------

def extract_playlist_id(url: str) -> str | None:
    match = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    # allow raw ID paste
    if re.fullmatch(r"[a-zA-Z0-9_-]+", url.strip()):
        return url.strip()
    return None


def fetch_youtube_playlist_titles(playlist_id: str, api_key: str) -> list[str]:
    """Return a list of video titles (and channel/uploader) for a public playlist."""
    titles = []
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet",
        "maxResults": 50,
        "playlistId": playlist_id,
        "key": api_key,
    }
    while True:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "YouTube API error"))
        for item in data.get("items", []):
            snippet = item["snippet"]
            title = snippet.get("title", "")
            channel = snippet.get("videoOwnerChannelTitle", snippet.get("channelTitle", ""))
            if title and title != "Deleted video" and title != "Private video":
                titles.append({"title": title, "channel": channel})
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token
    return titles


def clean_title(raw_title: str, channel: str) -> str:
    """Turn a messy YouTube video title into a cleaner 'artist song' search query."""
    t = raw_title

    # Remove bracketed/parenthetical noise
    noise_patterns = [
        r"\(official.*?\)", r"\[official.*?\]",
        r"\(lyrics?.*?\)", r"\[lyrics?.*?\]",
        r"\(audio.*?\)", r"\[audio.*?\]",
        r"\(music video.*?\)", r"\[music video.*?\]",
        r"\(visualizer.*?\)", r"\[visualizer.*?\]",
        r"\(hd\)", r"\[hd\]", r"\(4k\)", r"\[4k\]",
        r"\(full song.*?\)", r"\[full song.*?\]",
        r"\(video.*?\)", r"\[video.*?\]",
        r"\|.*", # anything after a pipe is often channel/extra info
    ]
    for p in noise_patterns:
        t = re.sub(p, "", t, flags=re.IGNORECASE)

    t = re.sub(r"\s+", " ", t).strip(" -|:")

    # If title has "Artist - Song" pattern, keep as is (Spotify search handles this well)
    return t.strip()


# ---------------------------------------------------------------------------
# HELPERS: Spotify
# ---------------------------------------------------------------------------

def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        cache_path=None,
        show_dialog=True,
    )


def search_track(sp: spotipy.Spotify, query: str):
    try:
        results = sp.search(q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if items:
            return items[0]
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# APP STATE / AUTH FLOW
# ---------------------------------------------------------------------------

if "token_info" not in st.session_state:
    st.session_state.token_info = None

st.title("🎵 YouTube Playlist → Spotify Playlist")
st.write(
    "Paste a YouTube playlist link, log in with Spotify, and this app will "
    "recreate the playlist on Spotify by matching each video to a track."
)

missing_config = []
if not YOUTUBE_API_KEY:
    missing_config.append("YOUTUBE_API_KEY")
if not SPOTIFY_CLIENT_ID:
    missing_config.append("SPOTIFY_CLIENT_ID")
if not SPOTIFY_CLIENT_SECRET:
    missing_config.append("SPOTIFY_CLIENT_SECRET")
if not SPOTIFY_REDIRECT_URI:
    missing_config.append("SPOTIFY_REDIRECT_URI")

if missing_config:
    st.error(
        "Missing configuration in Streamlit secrets: " + ", ".join(missing_config) +
        ". See the README for setup instructions."
    )
    st.stop()

# Handle Spotify OAuth redirect callback
query_params = st.query_params
sp_oauth = get_spotify_oauth()

if st.session_state.token_info is None:
    if "code" in query_params:
        code = query_params["code"]
        try:
            token_info = sp_oauth.get_access_token(code, as_dict=True, check_cache=False)
            st.session_state.token_info = token_info
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Spotify login failed: {e}")
    else:
        auth_url = sp_oauth.get_authorize_url()
        st.link_button("🔗 Log in with Spotify", auth_url, type="primary")
        st.stop()

# Refresh token if expired
token_info = st.session_state.token_info
if sp_oauth.is_token_expired(token_info):
    token_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
    st.session_state.token_info = token_info

sp = spotipy.Spotify(auth=token_info["access_token"])
current_user = sp.current_user()
st.success(f"Logged in to Spotify as **{current_user['display_name']}**")

if st.button("Log out of Spotify"):
    st.session_state.token_info = None
    st.rerun()

st.divider()

playlist_url = st.text_input("YouTube playlist URL", placeholder="https://www.youtube.com/playlist?list=...")
new_playlist_name = st.text_input("New Spotify playlist name", placeholder="e.g. My YouTube Favorites")
make_private = st.checkbox("Make playlist private", value=False)

if st.button("Convert playlist ▶", type="primary", disabled=not (playlist_url and new_playlist_name)):
    playlist_id = extract_playlist_id(playlist_url)
    if not playlist_id:
        st.error("Couldn't find a playlist ID in that URL. Paste the full YouTube playlist link.")
        st.stop()

    with st.spinner("Fetching videos from YouTube..."):
        try:
            videos = fetch_youtube_playlist_titles(playlist_id, YOUTUBE_API_KEY)
        except Exception as e:
            st.error(f"Error fetching YouTube playlist: {e}")
            st.stop()

    if not videos:
        st.warning("No videos found in that playlist (it may be private or empty).")
        st.stop()

    st.info(f"Found {len(videos)} videos. Searching for matches on Spotify...")

    progress = st.progress(0)
    matched_uris = []
    matched_display = []
    unmatched_display = []

    for i, video in enumerate(videos):
        query = clean_title(video["title"], video["channel"])
        track = search_track(sp, query)
        if not track:
            # fallback: try with channel name appended as likely artist
            fallback_query = f"{video['channel']} {clean_title(video['title'], '')}"
            track = search_track(sp, fallback_query)

        if track:
            matched_uris.append(track["uri"])
            artists = ", ".join(a["name"] for a in track["artists"])
            matched_display.append(f"✅ {video['title']}  →  **{track['name']}** by {artists}")
        else:
            unmatched_display.append(f"❌ {video['title']}")

        progress.progress((i + 1) / len(videos))
        time.sleep(0.05)  # gentle pacing to avoid rate limits

    # Create the playlist and add tracks
    with st.spinner("Creating Spotify playlist..."):
        new_playlist = sp.user_playlist_create(
            user=current_user["id"],
            name=new_playlist_name,
            public=not make_private,
            description="Converted from a YouTube playlist",
        )
        for chunk_start in range(0, len(matched_uris), 100):
            sp.playlist_add_items(new_playlist["id"], matched_uris[chunk_start:chunk_start + 100])

    st.success(
        f"Done! Matched {len(matched_uris)} of {len(videos)} tracks. "
        f"[Open playlist on Spotify]({new_playlist['external_urls']['spotify']})"
    )

    with st.expander(f"Matched tracks ({len(matched_display)})", expanded=True):
        for line in matched_display:
            st.markdown(line)

    if unmatched_display:
        with st.expander(f"Not found on Spotify ({len(unmatched_display)})"):
            for line in unmatched_display:
                st.markdown(line)
