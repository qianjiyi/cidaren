import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import a as quiz
    from config import get_missing_auth_fields, get_runtime_config
else:  # pragma: no cover
    from . import a as quiz
    from .config import get_missing_auth_fields, get_runtime_config

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in {"class", "study"}:
        task_kind = sys.argv[1]
        args = sys.argv[2:]
    else:
        task_kind = "class"
        args = sys.argv[1:]

    config = get_runtime_config()
    missing = get_missing_auth_fields(config)
    if missing:
        raise SystemExit(f"缺少必要配置: {', '.join(missing)}")
    c = quiz.Client(
        config["USERTOKEN"],
        config["ABC"],
        config["AUTH_V"],
        ua=config.get("USER_AGENT", ""),
    )
    if task_kind == "study":
        task_id = int(args[0])
        list_id = args[1]
        course_id = args[2] if len(args) >= 3 else config.get("COURSE_ID", "CET4_v2")
        task_type = int(args[3]) if len(args) >= 4 else 3
        grade = int(args[4]) if len(args) >= 5 else int(config.get("STUDY_GRADE", "2") or 2)
        quiz.run_study_full(c, task_id=task_id, course_id=course_id, list_id=list_id, task_type=task_type, grade=grade)
    else:
        task_id = int(args[0])
        release_id = int(args[1])
        quiz.run_full(c, task_id=task_id, release_id=release_id)
