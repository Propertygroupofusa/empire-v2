#!/usr/bin/env python3
"""Verify Study Assistant MVP is properly configured."""
import os
import sys
from pathlib import Path

def check(condition, description):
    status = "✅" if condition else "❌"
    print(f"{status} {description}")
    return condition

def main():
    print("\n📚 Study Assistant Setup Verification\n")

    all_good = True

    # Check files exist
    print("Checking files...")
    all_good &= check(
        Path("routers/study.py").exists(),
        "routers/study.py exists"
    )
    all_good &= check(
        Path("static/study.html").exists(),
        "static/study.html exists"
    )
    all_good &= check(
        Path("STUDY_ASSISTANT_README.md").exists(),
        "STUDY_ASSISTANT_README.md exists"
    )

    # Check models
    print("\nChecking models.py...")
    with open("models.py", "r") as f:
        models_content = f.read()
    all_good &= check(
        "class StudyUser" in models_content,
        "StudyUser model defined"
    )
    all_good &= check(
        "class StudyMaterial" in models_content,
        "StudyMaterial model defined"
    )

    # Check main.py
    print("\nChecking main.py...")
    with open("main.py", "r") as f:
        main_content = f.read()
    all_good &= check(
        "from routers import" in main_content and "study" in main_content,
        "study router imported in main.py"
    )
    all_good &= check(
        "app.include_router(study.router" in main_content,
        "study router included in app"
    )
    all_good &= check(
        "StaticFiles" in main_content,
        "StaticFiles imported for static assets"
    )
    all_good &= check(
        "/study-app" in main_content,
        "Study app endpoint defined"
    )

    # Check environment
    print("\nChecking environment...")
    has_api_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    all_good &= check(
        has_api_key,
        f"ANTHROPIC_API_KEY set" + (f" ({os.getenv('ANTHROPIC_API_KEY')[:20]}...)" if has_api_key else "")
    )

    # Check dependencies
    print("\nChecking dependencies...")
    try:
        import anthropic
        all_good &= check(True, "anthropic package installed")
    except ImportError:
        all_good &= check(False, "anthropic package NOT installed (run: pip install anthropic)")

    try:
        import fastapi
        all_good &= check(True, "fastapi installed")
    except ImportError:
        all_good &= check(False, "fastapi NOT installed")

    try:
        import sqlalchemy
        all_good &= check(True, "sqlalchemy installed")
    except ImportError:
        all_good &= check(False, "sqlalchemy NOT installed")

    # Summary
    print("\n" + "="*50)
    if all_good:
        print("✅ All checks passed! Study Assistant is ready.")
        print("\nNext steps:")
        print("1. python main.py")
        print("2. Visit http://localhost:8000/study-app")
        print("3. Upload a textbook image and generate study materials")
        print("\nSee STUDY_ASSISTANT_README.md for full docs.")
        return 0
    else:
        print("❌ Some checks failed. See above for details.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
