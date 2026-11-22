#!/usr/bin/env python3
"""Record every camera in tai88-cameras.json concurrently (default: 1 minute).

Requires Python 3.9+ and ffmpeg with libx264, drawtext, hqdn3d and unsharp.
Capture first, then encode: encoding load cannot slow down network acquisition.
The overlay starts at request time (UTC+08) and follows continuous video time.
Connection gaps are removed; the overlay is not camera exposure time.
Missing intervals are omitted; no placeholder images or log/report files are written.
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

TAIPEI = dt.timezone(dt.timedelta(hours=8))


def ffmpeg(args):
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])



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
    deadline = start + args.duration_seconds
    frames = []
    connection = 0
    try:
        with tempfile.TemporaryDirectory(prefix=name + '-', dir=folder) as tmp:
            tmp = Path(tmp)
            while time.monotonic() < deadline and not stop.is_set():
                connection += 1
                try:
                    request = urllib.request.Request(camera['VideoStreamURL'], headers={'User-Agent': 'tai88-recorder/1.0'})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        buf = b''
                        while time.monotonic() < deadline and not stop.is_set():
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            buf += chunk
                            if len(buf) > 8 * 1024 * 1024:
                                raise ValueError('No valid JPEG found within 8 MB')
                            while True:
                                a = buf.find(b'\xff\xd8')
                                b = buf.find(b'\xff\xd9', max(a, 0))
                                if a < 0 or b < 0:
                                    break
                                elapsed = time.monotonic() - start
                                jpeg, buf = buf[a:b + 2], buf[b + 2:]
                                if elapsed >= args.duration_seconds:
                                    break
                                path = tmp / f'{len(frames):07d}.jpg'
                                path.write_bytes(jpeg)
                                frames.append((path, elapsed, connection))
                except (OSError, ValueError):
                    pass
                stop.wait(min(0.2, max(0, deadline - time.monotonic())))
            if not frames:
                return False
            intervals = [b[1] - a[1] for a, b in zip(frames, frames[1:])
                         if a[2] == b[2] and 0 < b[1] - a[1] <= 2.0]
            normal_interval = statistics.median(intervals) if intervals else 1 / args.fps
            segments = []
            timeline = 0.0
            for i, (path, timestamp, session) in enumerate(frames):
                following = frames[i + 1] if i + 1 < len(frames) else None
                # A new HTTP request starts immediately after the previous frames
                # in the output, regardless of the wall-clock reconnection delay.
                same_session = following is not None and following[2] == session
                interval = following[1] - timestamp if same_session else normal_interval
                duration = interval if 0 < interval <= 2.0 else normal_interval
                duration = min(duration, max(0, args.duration_seconds - timestamp))
                if duration <= 0:
                    continue
                segments.append((path, duration))
                timeline += duration
            length = timeline
            manifest = tmp / 'frames.txt'
            lines = []
            for path, duration in segments:
                # Paths are generated internally; escape apostrophes for concat syntax.
                escaped = str(path).replace("'", "'\\''")
                lines.extend([f"file '{escaped}'", 'option framerate 1000', f'duration {duration:.6f}'])
            escaped = str(segments[-1][0]).replace("'", "'\\''")
            lines.extend([f"file '{escaped}'", 'option framerate 1000'])
            manifest.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            # gmtime + eight hours is Taiwan time, independent of OS timezone.
            timestamp_text = f"%{{pts\\:gmtime\\:{epoch + 8 * 3600:.6f}}} UTC+08"
            filters = (
                'scale=1280:960:flags=lanczos,setsar=1,'
                f'fps={args.fps},hqdn3d=1.5:1.5:3:3,'
                'unsharp=5:5:0.6:5:5:0,'
                f"drawtext=fontfile='{args.font}':text='{timestamp_text}':"
                'x=24:y=24:fontsize=40:fontcolor=white:box=1:boxcolor=black:boxborderw=12,'
                'format=yuv420p'
            )
            output = folder / f'{name}.mp4'
            partial = folder / f'{name}.partial.mp4'
            ffmpeg(['-threads', '1', '-filter_threads', '1', '-f', 'concat', '-safe', '0',
                    '-reinit_filter', '0', '-i', str(manifest), '-vf', filters, '-t', str(length), '-an',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '1',
                    '-movflags', '+faststart', str(partial)])
            partial.replace(output)
            print(f'{ident}: {output}', flush=True)
            return not stop.is_set()
    except Exception as exc:
        # Errors are shown once on stderr, never persisted as log files.
        print(f'{ident}: {exc}', file=sys.stderr, flush=True)
        partial = folder / f'{name}.partial.mp4'
        partial.unlink(missing_ok=True)
        return False


def main():
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=base / 'tai88-cameras.json')
    parser.add_argument('--output', type=Path, default=base / 'recordings')
    parser.add_argument('--duration', type=float, default=2,
                        help='Recording window in MINUTES (default: 1; --duration 10 = 10 minutes)')
    parser.add_argument('--fps', type=int, default=10, help='Output FPS (does not add source detail)')
    parser.add_argument('--camera-id', action='append', help='Optional camera subset; repeat to select more')
    parser.add_argument('--font', type=Path, default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'))
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0 or not 1 <= args.fps <= 60:
        parser.error('duration must be positive and finite; fps must be between 1 and 60')
    if not shutil.which('ffmpeg') or not args.font.is_file():
        parser.error('ffmpeg and a font file are required (use --font to specify a font)')
    args.duration_seconds = args.duration * 60
    if not math.isfinite(args.duration_seconds):
        parser.error('duration is too large')
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
    end_time = start_time + dt.timedelta(seconds=args.duration_seconds)
    folder = args.output.resolve() / (
        f'{start_time:%Y%m%d_%H%M%S}--{end_time:%Y%m%d_%H%M%S}'
    )
    folder.mkdir(parents=True, exist_ok=False)
    ready, stop = threading.Event(), threading.Event()
    clock = []
    results = []
    print(f'Recording {len(cameras)} cameras for {args.duration:g} minutes', flush=True)
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
