import os
import tempfile
import shutil
import json
import subprocess
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

app = FastAPI(title="Video Subtitler API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Video Subtitler Backend Running"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Simple health check + database envs visibility (if configured)"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Used",
        "database_url": "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set",
        "database_name": "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": [],
    }
    return response


# -------- Utility helpers -------- #

def _save_upload_to_temp(upload: UploadFile) -> str:
    suffix = os.path.splitext(upload.filename or "")[1] or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(tmp_path, "wb") as out:
        shutil.copyfileobj(upload.file, out)
    return tmp_path


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def _ffprobe_duration(path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _generate_demo_srt(duration: float = 9.0) -> str:
    # Simple Hinglish demo subtitles spread across duration
    segments = [
        (0.0, min(3.0, duration), "Namaste! Yeh demo transcription hai."),
        (3.0, min(6.0, duration), "Aap yahan subtitles edit kar sakte ho."),
        (6.0, duration, "Jab ready ho, burn-in karke download kar lo.")
    ]

    def ts(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    lines = []
    for i, (start, end, text) in enumerate(segments, start=1):
        if end <= start:
            end = start + 1
        lines.append(str(i))
        lines.append(f"{ts(start)} --> {ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


# -------- API Endpoints -------- #

@app.post("/api/transcribe")
async def transcribe_video(file: UploadFile = File(...)):
    """
    Accepts a video file and returns auto-generated subtitles (Hinglish) as SRT.
    Placeholder logic: generates demo subtitles. If ffprobe is available, uses video duration.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    temp_path = _save_upload_to_temp(file)
    duration = _ffprobe_duration(temp_path) or 9.0
    srt_text = _generate_demo_srt(duration)

    # Clean up temp file
    try:
        os.remove(temp_path)
    except Exception:
        pass

    return JSONResponse({"srt": srt_text, "duration": duration})


@app.post("/api/upload-srt")
async def upload_srt(file: UploadFile = File(...)):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    content = (await file.read()).decode("utf-8", errors="ignore")
    return {"srt": content}


@app.post("/api/burn")
async def burn_subtitles(
    file: UploadFile = File(...),
    srt: str = Form(...),
    position: str = Form("bottom"),
    color: str = Form("#FFFFFF"),
    font_size: int = Form(28),
    bg_opacity: float = Form(0.4),
):
    """
    Burn subtitles into the video using ffmpeg if available.
    If ffmpeg isn't available, returns the original video (simulated result).
    position: bottom | top | left | right | center
    color: hex like #RRGGBB
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    video_path = _save_upload_to_temp(file)

    # Write SRT to temp file
    srt_fd, srt_path = tempfile.mkstemp(suffix=".srt")
    os.close(srt_fd)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt)

    # Prepare output path
    out_fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(out_fd)

    # Position mapping via ASS Alignment (1-9)
    alignment_map = {
        "bottom": 2,  # bottom-center
        "top": 8,     # top-center
        "left": 4,    # middle-left
        "right": 6,   # middle-right
        "center": 5,  # middle-center
    }
    align = alignment_map.get(position, 2)

    # Convert hex color #RRGGBB to ASS BGR format with alpha
    def hex_to_ass(hex_color: str, alpha: float = 0.0) -> str:
        hex_color = hex_color.lstrip('#')
        if len(hex_color) != 6:
            hex_color = "FFFFFF"
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        # ASS uses &HAABBGGRR
        aa = int(max(0, min(1, alpha)) * 255)
        return f"&H{aa:02X}{b:02X}{g:02X}{r:02X}"

    primary = hex_to_ass(color, 0.0)
    back = hex_to_ass("000000", 1.0 - max(0.0, min(1.0, bg_opacity)))

    force_style = f"Alignment={align},Fontsize={font_size},PrimaryColour={primary},BackColour={back},Outline=1,BorderStyle=3"

    used_ffmpeg = False
    if _ffmpeg_available():
        try:
            # Use subtitles filter with force_style
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-vf",
                f"subtitles={srt_path}:force_style='{force_style}'",
                "-c:a",
                "copy",
                out_path,
            ]
            subprocess.run(cmd, check=True)
            used_ffmpeg = True
        except Exception as e:
            # Fallback to original video if burning fails
            shutil.copy(video_path, out_path)
    else:
        shutil.copy(video_path, out_path)

    # Cleanup temp inputs
    try:
        os.remove(video_path)
        os.remove(srt_path)
    except Exception:
        pass

    filename = "subtitled.mp4" if used_ffmpeg else "video.mp4"
    return FileResponse(out_path, media_type="video/mp4", filename=filename)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
