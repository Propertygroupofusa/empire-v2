"""
YOUTUBE SCHEDULER - Automatic Video Generation & Posting
Runs on a schedule: Generate 3 videos per week, auto-post to YouTube
Zero manual intervention after setup
"""

import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import List
import schedule
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SCHEDULER] %(message)s"
)
log = logging.getLogger("scheduler")

DB_PATH = "youtube_pipeline.db"

# ============================================================================
# CONTENT TOPICS (Expand these for more variety)
# ============================================================================

CONTENT_TOPICS = {
    "math": [
        "How to Multiply Fractions",
        "Understanding Percentages",
        "Solving Equations Step by Step",
        "Geometry Basics: Shapes and Angles",
        "Order of Operations (PEMDAS)",
        "Decimal and Fraction Conversions",
        "Introduction to Algebra",
        "Basic Trigonometry",
    ],
    "science": [
        "Photosynthesis Explained",
        "The Water Cycle",
        "Human Body Systems",
        "Periodic Table Elements",
        "Newton's Laws of Motion",
        "States of Matter",
        "Solar System Overview",
        "Ecosystems and Food Chains",
    ],
    "language": [
        "Spanish Vocabulary: Fruits",
        "Spanish Vocabulary: Animals",
        "French Basic Phrases",
        "English Grammar: Tenses",
        "Writing Better Sentences",
        "Punctuation Rules",
        "Spelling Tips",
    ],
    "history": [
        "Ancient Egypt Timeline",
        "American Revolution Summary",
        "Ancient Rome History",
        "Medieval Times Overview",
        "World War 1 Causes",
        "Industrial Revolution",
        "Renaissance Period",
    ],
    "study_skills": [
        "Best Study Tips for Exams",
        "How to Take Better Notes",
        "Memory Techniques That Work",
        "Time Management for Students",
        "Test Anxiety Solutions",
        "Reading Comprehension Tips",
        "Essay Writing Guide",
    ]
}


class YouTubeScheduler:
    """Automated scheduler for YouTube video generation"""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        self.topic_index = 0
        self.total_topics = sum(len(v) for v in CONTENT_TOPICS.values())

    def get_next_topics(self, count: int = 3) -> List[str]:
        """Get next N topics in rotation"""
        topics = []
        topics_list = []

        # Flatten topics dict
        for category, category_topics in CONTENT_TOPICS.items():
            for topic in category_topics:
                topics_list.append(topic)

        # Get next N topics (cycling through)
        for i in range(count):
            idx = (self.topic_index + i) % len(topics_list)
            topics.append(topics_list[idx])

        self.topic_index = (self.topic_index + count) % len(topics_list)
        return topics

    def get_video_count(self) -> int:
        """Get total videos generated so far"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM youtube_videos WHERE status != 'failed'")
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def log_schedule(self, action: str, details: str = ""):
        """Log scheduling actions"""
        log.info(f"[SCHEDULE] {action} - {details}")

        # Store in database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO pipeline_logs (step, status, message) VALUES (?, ?, ?)",
            ("scheduler", action, details)
        )
        conn.commit()
        conn.close()

    def schedule_weekly_generation(self):
        """Schedule 3 videos per week (Mon, Wed, Fri at 9am)"""
        schedule.every().monday.at("09:00").do(self.run_scheduled_generation)
        schedule.every().wednesday.at("09:00").do(self.run_scheduled_generation)
        schedule.every().friday.at("09:00").do(self.run_scheduled_generation)
        self.log_schedule("SCHEDULED", "3 generations/week (Mon/Wed/Fri 9am)")

    def run_scheduled_generation(self):
        """Run the scheduled video generation"""
        self.log_schedule("GENERATION_STARTED", f"Total videos so far: {self.get_video_count()}")

        try:
            # Import here to avoid circular dependency
            from youtube_auto_pipeline import run_batch_pipeline

            topics = self.get_next_topics(3)
            log.info(f"Generating videos for: {topics}")

            # Run async pipeline
            asyncio.run(run_batch_pipeline(topics, "intermediate"))

            self.log_schedule("GENERATION_SUCCESS", f"Generated {len(topics)} videos")
            log.info(f"✅ Scheduled generation complete")

        except Exception as e:
            self.log_schedule("GENERATION_FAILED", str(e))
            log.error(f"❌ Scheduled generation failed: {e}")

    def run_forever(self):
        """Keep scheduler running"""
        log.info("=" * 60)
        log.info("YOUTUBE SCHEDULER STARTED")
        log.info("=" * 60)

        self.schedule_weekly_generation()

        log.info("\n📅 SCHEDULE:")
        log.info("  ⏰ Monday 9:00 AM - Generate 3 videos")
        log.info("  ⏰ Wednesday 9:00 AM - Generate 3 videos")
        log.info("  ⏰ Friday 9:00 AM - Generate 3 videos")
        log.info(f"  📊 Total video library: {self.total_topics} unique topics")
        log.info("\nScheduler is running. Press Ctrl+C to stop.\n")

        try:
            while True:
                schedule.run_pending()
                asyncio.sleep(60)

        except KeyboardInterrupt:
            log.info("\n📛 Scheduler stopped")


# ============================================================================
# QUICK COMMANDS
# ============================================================================

def generate_now():
    """Force immediate generation (useful for testing)"""
    scheduler = YouTubeScheduler()
    log.info("🚀 Forcing immediate generation...")
    scheduler.run_scheduled_generation()


def show_status():
    """Show current scheduler status"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM youtube_videos")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT status, COUNT(*) FROM youtube_videos GROUP BY status")
    breakdown = dict(cursor.fetchall())

    cursor.execute(
        "SELECT COUNT(*) FROM pipeline_logs WHERE step='scheduler' AND status='GENERATION_SUCCESS'"
    )
    successful_runs = cursor.fetchone()[0]

    conn.close()

    log.info("\n" + "=" * 60)
    log.info("YOUTUBE SCHEDULER STATUS")
    log.info("=" * 60)
    log.info(f"Total Videos Generated: {total}")
    log.info(f"Scheduled Generations: {successful_runs}")
    log.info(f"Status Breakdown:")
    for status, count in breakdown.items():
        log.info(f"  • {status}: {count}")
    log.info("=" * 60 + "\n")


def estimate_revenue():
    """Estimate potential revenue"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT SUM(views) FROM youtube_videos WHERE youtube_video_id IS NOT NULL")
    total_views = cursor.fetchone()[0] or 0

    conn.close()

    cpms = {
        "conservative": 0.25,
        "moderate": 1.0,
        "optimistic": 2.0,
    }

    revenue = {
        "conservative": total_views * cpms["conservative"] / 1000,
        "moderate": total_views * cpms["moderate"] / 1000,
        "optimistic": total_views * cpms["optimistic"] / 1000,
    }

    log.info("\n" + "=" * 60)
    log.info("REVENUE ESTIMATE")
    log.info("=" * 60)
    log.info(f"Total Views: {total_views:,}")
    log.info(f"CPM Range: ${cpms['conservative']}-${cpms['optimistic']}")
    log.info(f"Revenue Estimate:")
    log.info(f"  • Conservative: ${revenue['conservative']:.2f}")
    log.info(f"  • Moderate: ${revenue['moderate']:.2f}")
    log.info(f"  • Optimistic: ${revenue['optimistic']:.2f}")
    log.info("=" * 60 + "\n")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "start":
            scheduler = YouTubeScheduler()
            scheduler.run_forever()

        elif command == "generate-now":
            generate_now()

        elif command == "status":
            show_status()

        elif command == "revenue":
            estimate_revenue()

        else:
            print(f"Unknown command: {command}")
            print("\nAvailable commands:")
            print("  python youtube_scheduler.py start          - Run scheduler")
            print("  python youtube_scheduler.py generate-now   - Generate videos immediately")
            print("  python youtube_scheduler.py status         - Show current status")
            print("  python youtube_scheduler.py revenue        - Estimate revenue")

    else:
        print("\nYouTube Scheduler - Usage:")
        print("  python youtube_scheduler.py start              # Run scheduler (3 videos/week)")
        print("  python youtube_scheduler.py generate-now       # Generate videos now")
        print("  python youtube_scheduler.py status             # Show statistics")
        print("  python youtube_scheduler.py revenue            # Estimate revenue")
