import requests
import logging
from concurrent.futures import ThreadPoolExecutor
import re
import json

logger = logging.getLogger(__name__)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) TIDAL/2.38.5 Chrome/126.0.6478.127 Electron/31.2.1 Safari/537.36",
    "x-tidal-token": "49YxDN9a2aFV6RTG",
    "Accept": "application/json"
}

V2_HEADERS = HEADERS.copy()
V2_HEADERS.update({
    "x-tidal-client-version": "2025.10.29",
    "Origin": "https://desktop.tidal.com",
    "Referer": "https://desktop.tidal.com/",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN",
    "sec-ch-ua-platform": '"macOS"'
})

LIMIT = 50

def _format_tidal_artist_work(item: dict, item_type: str) -> dict:
    if not item or not item.get("id"):
        return None
    
    artists_array = []
    for artist in item.get("artists", []):
        if artist.get('id'):
            artists_array.append({
                "id": str(artist.get("id")),
                "name": artist.get("name"),
                "source": "tidal"
            })

    media_metadata = item.get('mediaMetadata', {}) or {}
    tags = media_metadata.get('tags', []) or []
    cover_uuid = item.get('cover')
    
    return {
        "id": str(item.get("id")),
        "name": item.get("title"),
        "title": item.get("title"),
        "releaseDate": item.get("releaseDate"),
        "cover": cover_uuid,
        "coverUrl": f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/320x320.jpg" if cover_uuid else "",
        "artists": artists_array,
        "source": "tidal",
        "rawUrl": f"https://listen.tidal.com/album/{item.get('id')}",
        "type": "album",
        "isSingle": item_type == 'single',
        "mediaMetadata": media_metadata, 
        "isHires": "HIRES_LOSSLESS" in tags,
        "isAtmos": "DOLBY_ATMOS" in tags,
        "explicit": item.get("explicit", False)
    }

def get_tidal_artist_works_page(artist_id: str, work_type: str, offset: int, region: str, tidal_token: str):
    headers_v1 = HEADERS.copy()
    headers_v1["authorization"] = f"Bearer {tidal_token}"
    
    headers_v2 = V2_HEADERS.copy()
    headers_v2["authorization"] = f"Bearer {tidal_token}"
    
    url_map = {
        "albums": f"https://api.tidal.com/v1/artists/{artist_id}/albums?limit={LIMIT}&offset={offset}&countryCode={region}",
        "singles": f"https://api.tidal.com/v1/artists/{artist_id}/albums?filter=EPSANDSINGLES&limit={LIMIT}&offset={offset}&countryCode={region}",
        "videos": f"https://api.tidal.com/v1/artists/{artist_id}/videos?limit={LIMIT}&offset={offset}&countryCode={region}",
        "related_artists": f"https://api.tidal.com/v1/artists/{artist_id}/similar?limit={LIMIT}&offset={offset}&countryCode={region}"
    }

    if work_type not in url_map:
        return {"error": "Invalid work type"}
    
    items = []
    total_items = 0

    if work_type == "related_artists":
        all_items = []
        try:
            resp_v1 = requests.get(url_map[work_type], headers=headers_v1, timeout=20)
            if resp_v1.status_code == 200:
                data_v1 = resp_v1.json()
                items_v1 = data_v1.get("items", [])
                all_items.extend(items_v1)
                total_items = data_v1.get("totalNumberOfItems", len(items_v1))
        except Exception:
            pass
            
        if not all_items:
            try:
                v2_url = f"https://api.tidal.com/v2/artist/ARTIST_SIMILAR_ARTISTS/view-all?itemId={artist_id}&locale=en_US&countryCode={region}&deviceType=DESKTOP&platform=DESKTOP&limit=50&offset={offset}"
                resp_v2 = requests.get(v2_url, headers=headers_v2, timeout=20)
                if resp_v2.status_code == 200:
                    data_v2 = resp_v2.json()
                    items_v2 = [i.get("data") for i in data_v2.get("items", []) if i.get("type") == "ARTIST" and i.get("data")]
                    all_items.extend(items_v2)
                    total_items = len(items_v2)
            except Exception:
                pass

        unique_items = {str(i["id"]): i for i in all_items if i.get("id")}.values()
        formatted_items = []
        for item in unique_items:
             pic = item.get("picture")
             formatted_items.append({
                "id": str(item.get("id")), 
                "name": item.get("name"), 
                "type": "artist", 
                "source": "tidal",
                "coverUrl": f"https://resources.tidal.com/images/{pic.replace('-', '/')}/320x320.jpg" if pic else "",
                "rawUrl": f"https://listen.tidal.com/artist/{item.get('id')}"
             })
        
        return {
            "items": formatted_items,
            "next_offset": offset + LIMIT if len(unique_items) >= LIMIT else None,
            "total_items": total_items
        }

    try:
        resp = requests.get(url_map[work_type], headers=headers_v1, timeout=20)
        resp.raise_for_status()
        json_data = resp.json()
        items = json_data.get("items", []) or []
        total_items = json_data.get("totalNumberOfItems", len(items))
    except Exception as e:
        logger.error(f"Tidal fetch failed for {work_type}: {e}")
        return {"error": str(e)}

    formatted_items = []
    if work_type == "videos":
         for item in items:
            img_id = item.get("imageId")
            formatted_items.append({
                "id": str(item.get("id")), 
                "name": item.get("title"), 
                "title": item.get("title"),
                "type": "video", 
                "source": "tidal",
                "coverUrl": f"https://resources.tidal.com/images/{img_id.replace('-', '/')}/320x214.jpg" if img_id else "",
                "artists": [{"id": str(a.get("id")), "name": a.get("name")} for a in item.get("artists", [])], 
                "rawUrl": f"https://listen.tidal.com/video/{item.get('id')}"
            })
    else:
        for item in items:
            if fmt := _format_tidal_artist_work(item, 'single' if work_type == 'singles' else 'album'):
                formatted_items.append(fmt)
    
    return {
        "items": formatted_items,
        "next_offset": offset + LIMIT if len(items) == LIMIT and offset + LIMIT < total_items else None,
        "total_items": total_items
    }

def get_tidal_artist_details(artist_id: str, region: str, tidal_token: str) -> dict:
    try:
        headers = HEADERS.copy()
        headers["authorization"] = f"Bearer {tidal_token}"

        resp = requests.get(f"https://api.tidal.com/v1/artists/{artist_id}?countryCode={region}", headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        
        result = {
            "artist": data.get("name"), 
            "artist_id": artist_id,
            "description": data.get("biography", {}).get("text", ""),
            "rawUrl": f"https://listen.tidal.com/artist/{artist_id}",
            "picture_uuid": data.get("picture"),
            "image": f"https://resources.tidal.com/images/{data.get('picture', '').replace('-', '/')}/320x320.jpg" if data.get("picture") else ""
        }

        with ThreadPoolExecutor(max_workers=2) as exc:
            f_related = exc.submit(get_tidal_artist_works_page, artist_id, "related_artists", 0, region, tidal_token)
            related_res = f_related.result()

        result.update({
            "related_artists": related_res.get("items", []),
            "related_artists_next_offset": related_res.get("next_offset"),
            "related_artists_total": related_res.get("total_items"),
        })

        return result
    except Exception as e:
        logger.error(f"Artist details failed {artist_id}: {e}")
        return {"error": str(e)}

def extract_tidal_album_id(user_input: str) -> str:
    user_input = user_input.strip()
    match = re.search(r'(?:tidal\.com|listen\.tidal\.com)?(?:/album/)?(\d+)', user_input, re.IGNORECASE)
    return match.group(1) if match else (user_input if user_input.isdigit() else None)