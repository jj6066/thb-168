#!/usr/bin/env python3
"""Record every camera concurrently to the requested OUTPUT duration (default: 0.5 minutes).

Requires Python 3.9+, Pillow and ffmpeg with libx264, drawtext, hqdn3d and unsharp.
Save numbered JPEGs first, then encode exactly one video frame per image.
Frames play at --fps, independent of network arrival intervals.
Collect duration * 60 * fps images per camera; acquisition time is not limited.
Video duration = image count / fps. Slow sources take longer to collect.
The overlay starts at request time (UTC+08) and follows continuous video time.
Connection gaps are removed; the overlay is not camera exposure time.
Missing intervals are omitted; no placeholder images or log/report files are written.
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import io
import math
import random
import sys
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

from PIL import Image

TAIPEI = dt.timezone(dt.timedelta(hours=8))


def decode_jpeg(data):
    """Fully decode before counting; JPEG markers alone do not prove validity."""
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'JPEG':
            raise ValueError('Expected JPEG')
        image.load()
        return image.convert('RGB')


def images_to_mp4(image_dir, output, fps, filters):
    """Write saved images in numeric order, one image per encoded frame."""
    images = sorted(image_dir.glob('*.jpg'), key=lambda path: int(path.stem))
    if not images:
        raise ValueError('No images to encode')
    with decode_jpeg(images[0].read_bytes()) as first:
        size = first.size
    command = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-threads', '1', '-filter_threads', '1',
        '-f', 'rawvideo', '-pixel_format', 'rgb24',
        '-video_size', f'{size[0]}x{size[1]}', '-framerate', str(fps),
        '-i', 'pipe:0',
        '-vf', filters, '-frames:v', str(len(images)), '-fps_mode', 'passthrough',
        '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
        '-threads', '1', '-movflags', '+faststart', str(output),
    ]
    # Temporary stderr capture prevents pipe blocking; no log file is retained.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=errors)
        try:
            for image_path in images:
                with decode_jpeg(image_path.read_bytes()) as image:
                    if image.size != size:
                        image = image.resize(size, Image.Resampling.LANCZOS)
                    process.stdin.write(image.tobytes())
            process.stdin.close()
            returncode = process.wait()
        except BrokenPipeError:
            returncode = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            if not process.stdin.closed:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
        if returncode:
            errors.seek(0)
            raise RuntimeError(errors.read().decode('utf-8', errors='replace')[-3000:])
    return len(images)


def camera_sort_key(camera):
    km, meters = camera['LocationMile'].upper().split('K+')
    return (int(km) * 1000 + int(meters),
            float(camera['PositionLon']), camera['CCTVID'])


def prepare_cameras(cameras):
    # Assign stable route-wide numbering before applying --camera-id selection.
    result = []
    for index, camera in enumerate(sorted(cameras, key=camera_sort_key), 1):
        camera = dict(camera)
        km, meters = camera['LocationMile'].upper().split('K+')
        direction = {'E': '東向', 'W': '西向'}.get(
            camera.get('RoadDirection'), '方向未標示')
        ident = re.sub(r'[^a-zA-Z0-9_-]', '_', camera['CCTVID'])
        camera['_filename'] = (
            f'{index:02d}_台88_{int(km):03d}K+{int(meters):03d}_{direction}_{ident}'
        )
        result.append(camera)
    return result


def record(camera, args, folder, ready, clock, stop):
    ident = camera['CCTVID']
    name = camera['_filename']
    ready.wait()
    start, epoch = clock
    target_frames = args.target_frames
    next_progress = time.monotonic() + 60
    frame_count = 0
    connection = 0
    empty_connections = 0
    tmp = Path(tempfile.mkdtemp(prefix=name + '-', dir=folder))
    try:
        while (frame_count < target_frames and not stop.is_set()
               and (args.max_requests is None or connection < args.max_requests)):
            connection += 1
            frames_before = frame_count
            try:
                request = urllib.request.Request(camera['VideoStreamURL'], headers={'User-Agent': 'tai88-recorder/1.0'})
                with urllib.request.urlopen(request, timeout=10) as response:
                    buf = b''
                    while frame_count < target_frames and not stop.is_set():
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        buf += chunk
                        if len(buf) > 8 * 1024 * 1024:
                            raise ValueError('No valid JPEG found within 8 MB')
                        while frame_count < target_frames and not stop.is_set():
                            a = buf.find(b'\xff\xd8')
                            b = buf.find(b'\xff\xd9', max(a, 0))
                            if a < 0 or b < 0:
                                break
                            jpeg, buf = buf[a:b + 2], buf[b + 2:]
                            try:
                                with decode_jpeg(jpeg):
                                    pass
                            except (OSError, ValueError):
                                continue
                            path = tmp / f'{frame_count:07d}.jpg'
                            path.write_bytes(jpeg)
                            frame_count += 1
                            if time.monotonic() >= next_progress:
                                print(f'{ident}: {frame_count}/{target_frames} images '
                                      f'({frame_count / args.fps:.1f}s of output)', flush=True)
                                next_progress = time.monotonic() + 60
            except (OSError, ValueError):
                pass
            if (frame_count < target_frames and not stop.is_set()
                    and (args.max_requests is None or connection < args.max_requests)):
                empty_connections = empty_connections + 1 if frame_count == frames_before else 0
                # Space reconnects out; repeated empty responses back off further.
                delay = min(30, 2 * (2 ** min(empty_connections, 4)))
                stop.wait(delay + random.uniform(0, 1))
        if not frame_count:
            shutil.rmtree(tmp)
            return False
        # Each saved JPEG produces one frame. Do not use arrival timestamps,
        # concat durations, an fps filter, or an output -r that repeats/drops frames.
        # gmtime + eight hours is Taiwan time, independent of OS timezone.
        timestamp_text = f"%{{pts\\:gmtime\\:{epoch + 8 * 3600:.6f}}} UTC+08"
        filters = (
            'scale=640:480:flags=lanczos,setsar=1,'
            'hqdn3d=1.5:1.5:3:3,'
            'unsharp=5:5:0.6:5:5:0,'
            f"drawtext=fontfile='{args.font}':text='{timestamp_text}':"
            'x=24:y=24:fontsize=40:fontcolor=white:box=1:boxcolor=black:boxborderw=12,'
            'format=yuv420p'
        )
        output = folder / f'{name}.mp4'
        partial = folder / f'{name}.partial.mp4'
        images_to_mp4(tmp, partial, args.fps, filters)
        probe = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
            '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(partial),
        ]))
        encoded_frames = int(probe['streams'][0]['nb_read_frames'])
        if encoded_frames <= 0:
            raise RuntimeError('No decodable frames in encoded video')
        request_limit_reached = (args.max_requests is not None
                                 and connection >= args.max_requests and not stop.is_set())
        if frame_count != target_frames and not request_limit_reached:
            output = folder / f'{name}.incomplete.mp4'
        partial.replace(output)
        print(f'{ident}: {frame_count} images -> {encoded_frames} frames, '
              f'{encoded_frames / args.fps:.2f}s at {args.fps} FPS '
              f'({connection} HTTP requests): {output}', flush=True)
        shutil.rmtree(tmp)
        return frame_count == target_frames or request_limit_reached
    except Exception as exc:
        # Errors are shown once on stderr, never persisted as log files.
        print(f'{ident}: {exc}', file=sys.stderr, flush=True)
        print(f'{ident}: retained images: {tmp}; any .partial.mp4 is retained',
              file=sys.stderr, flush=True)
        return False


def main():
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=base / 'tai88-cameras.json')
    parser.add_argument('--output', type=Path, default=base / 'recordings')
    parser.add_argument('--duration', type=float, default=0.5,
                        help='OUTPUT duration in MINUTES (default: 0.5; 10 at 10 FPS = 6000 images)')
    parser.add_argument('--max-requests', type=int, default=1,
                        help='Maximum HTTP requests per camera; 1 disables reconnects (default: unlimited)')
    parser.add_argument('--fps', type=int, default=5, help='Image playback FPS; one image per frame (default: 10)')
    parser.add_argument('--camera-id', action='append', help='Optional camera subset; repeat to select more')
    parser.add_argument('--font', type=Path, default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'))
    args = parser.parse_args()
    if args.max_requests is not None and args.max_requests < 1:
        parser.error('max-requests must be at least 1')
    if not math.isfinite(args.duration) or args.duration <= 0 or not 1 <= args.fps <= 60:
        parser.error('duration must be positive and finite; fps must be between 1 and 60')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe') or not args.font.is_file():
        parser.error('ffmpeg, ffprobe and a font file are required (use --font to specify a font)')
    args.duration_seconds = args.duration * 60
    if not math.isfinite(args.duration_seconds):
        parser.error('duration is too large')
    frame_target = args.duration_seconds * args.fps
    if not math.isfinite(frame_target):
        parser.error('duration is too large')
    args.target_frames = round(frame_target)
    if args.target_frames < 1 or not math.isclose(frame_target, args.target_frames, rel_tol=0, abs_tol=1e-6):
        parser.error('duration * 60 * fps must be a positive whole number of frames')
    args.font = args.font.resolve()
    document = json.loads(args.input.read_text(encoding='utf-8-sig'))
    cameras = document['cameras'] if isinstance(document, dict) else document
    cameras = prepare_cameras(cameras)
    if args.camera_id:
        requested = set(args.camera_id)
        cameras = [c for c in cameras if c['CCTVID'] in requested]
        missing = requested - {c['CCTVID'] for c in cameras}
        if missing:
            parser.error(f'Unknown cameras: {sorted(missing)}')
    if not cameras or len({c['CCTVID'] for c in cameras}) != len(cameras):
        parser.error('Camera list must be nonempty with unique CCTVID values')
    for camera in cameras:
        if not camera.get('VideoStreamURL', '').startswith(('https://', 'http://')):
            parser.error(f"Missing HTTP stream URL: {camera['CCTVID']}")
    start_epoch = time.time()
    start_monotonic = time.monotonic()
    start_time = dt.datetime.fromtimestamp(start_epoch, TAIPEI)
    # Directory interval follows the continuous output timeline, not download time.
    end_time = start_time + dt.timedelta(seconds=args.duration_seconds)
    folder = args.output.resolve() / (
        f'{start_time:%Y%m%d_%H%M%S}--{end_time:%Y%m%d_%H%M%S}'
    )
    folder.mkdir(parents=True, exist_ok=False)
    ready, stop = threading.Event(), threading.Event()
    clock = []
    results = []
    print(f'Recording {len(cameras)} cameras: OUTPUT {args.duration:g} minutes, '
          f'{args.target_frames} images each at {args.fps} FPS', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cameras)) as pool:
        futures = [pool.submit(record, c, args, folder, ready, clock, stop) for c in cameras]
        clock.extend([start_monotonic, start_epoch])
        ready.set()
        try:
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        except KeyboardInterrupt:
            print('Stopping acquisition; saving received frames', file=sys.stderr)
            stop.set()
            results = [f.result() for f in futures]
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
