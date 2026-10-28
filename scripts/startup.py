import json
import os
from pathlib import Path

import bpy
import gpu


state_dir = Path(os.environ["BLENDER_STATE_DIR"])
threads = int(os.environ["BLENDER_THREADS"])
bpy.context.preferences.view.show_splash = False
bpy.context.scene.render.threads_mode = "FIXED"
bpy.context.scene.render.threads = threads
bpy.context.scene.cycles.device = "CPU"


def record_session():
    windows = [
        {
            "width": window.width,
            "height": window.height,
            "areas": [area.type for area in window.screen.areas],
        }
        for window in bpy.context.window_manager.windows
    ]
    report = {
        "session": os.environ["BLENDER_SESSION_ID"],
        "token": os.environ["BLENDER_SESSION_TOKEN"],
        "version": bpy.app.version_string,
        "background": bpy.app.background,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "display": os.environ.get("DISPLAY"),
        "renderer": gpu.platform.renderer_get(),
        "vendor": gpu.platform.vendor_get(),
        "opengl_version": gpu.platform.version_get(),
        "windows": windows,
        "objects": sorted(bpy.data.objects.keys()),
        "cycles_device": bpy.context.scene.cycles.device,
        "cpus": sorted(os.sched_getaffinity(0)),
        "config_dir": os.environ["BLENDER_USER_CONFIG"],
        "project_dir": os.getcwd(),
    }
    temporary = state_dir / "ready.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_dir / "ready.json")
    print("BLENDER_SESSION_READY " + json.dumps(report), flush=True)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()
    return None


bpy.app.timers.register(record_session, first_interval=2.0)
