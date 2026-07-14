"""
YOUTUBE AUTOMATION DASHBOARD
Real-time monitoring of video generation, uploads, and revenue
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dashboard")

DB_PATH = "youtube_pipeline.db"


class YouTubeDashboard:
    """Real-time dashboard for YouTube automation"""

    def __init__(self):
        self.db_path = DB_PATH

    def get_all_videos(self):
        """Get all videos with current status"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, title, status, youtube_video_id, views, created_at
            FROM youtube_videos
            ORDER BY created_at DESC
        """)
        videos = cursor.fetchall()
        conn.close()

        return videos

    def get_stats(self):
        """Get comprehensive statistics"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Total videos
        cursor.execute("SELECT COUNT(*) FROM youtube_videos")
        total_videos = cursor.fetchone()[0]

        # Videos by status
        cursor.execute("SELECT status, COUNT(*) FROM youtube_videos GROUP BY status")
        status_breakdown = dict(cursor.fetchall())

        # Total views
        cursor.execute("SELECT COALESCE(SUM(views), 0) FROM youtube_videos WHERE youtube_video_id IS NOT NULL")
        total_views = cursor.fetchone()[0]

        # Average views per video
        cursor.execute("""
            SELECT COALESCE(AVG(views), 0) FROM youtube_videos
            WHERE youtube_video_id IS NOT NULL AND views > 0
        """)
        avg_views = cursor.fetchone()[0]

        # Videos uploaded to YouTube
        cursor.execute("SELECT COUNT(*) FROM youtube_videos WHERE youtube_video_id IS NOT NULL")
        uploaded = cursor.fetchone()[0]

        # Recent activity
        cursor.execute("""
            SELECT COUNT(*) FROM youtube_videos
            WHERE created_at > datetime('now', '-7 days')
        """)
        videos_this_week = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM youtube_videos
            WHERE created_at > datetime('now', '-1 day')
        """)
        videos_today = cursor.fetchone()[0]

        # Errors
        cursor.execute("SELECT COUNT(*) FROM youtube_videos WHERE status = 'failed'")
        failed = cursor.fetchone()[0]

        conn.close()

        return {
            "total_videos": total_videos,
            "status_breakdown": status_breakdown,
            "total_views": int(total_views),
            "avg_views": int(avg_views),
            "uploaded_to_youtube": uploaded,
            "videos_this_week": videos_this_week,
            "videos_today": videos_today,
            "failed_videos": failed,
        }

    def get_recent_logs(self, limit=20):
        """Get recent pipeline logs"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT video_id, step, status, message, timestamp
            FROM pipeline_logs
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
        logs = cursor.fetchall()
        conn.close()

        return logs

    def get_revenue_estimate(self):
        """Estimate revenue based on views"""
        stats = self.get_stats()
        total_views = stats["total_views"]

        cpms = {
            "conservative": 0.25,
            "moderate": 1.0,
            "optimistic": 2.0,
        }

        return {
            "total_views": total_views,
            "cpm_range": f"${cpms['conservative']}-${cpms['optimistic']}",
            "revenue": {
                "conservative": total_views * cpms["conservative"] / 1000,
                "moderate": total_views * cpms["moderate"] / 1000,
                "optimistic": total_views * cpms["optimistic"] / 1000,
            }
        }

    def get_pipeline_health(self):
        """Get health check of pipeline"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Success rate
        cursor.execute("SELECT COUNT(*) FROM youtube_videos WHERE status IN ('published', 'uploaded')")
        successful = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM youtube_videos")
        total = cursor.fetchone()[0]

        success_rate = (successful / total * 100) if total > 0 else 0

        # Average generation time
        cursor.execute("""
            SELECT AVG(
                (julianday(updated_at) - julianday(created_at)) * 24 * 60
            )
            FROM youtube_videos
            WHERE status IN ('published', 'uploaded')
        """)
        avg_time_minutes = cursor.fetchone()[0] or 0

        # Recent failures
        cursor.execute("""
            SELECT COUNT(*) FROM youtube_videos
            WHERE status = 'failed'
            AND created_at > datetime('now', '-7 days')
        """)
        recent_failures = cursor.fetchone()[0]

        conn.close()

        return {
            "success_rate": f"{success_rate:.1f}%",
            "avg_generation_time_minutes": int(avg_time_minutes),
            "recent_failures_7d": recent_failures,
            "health_status": "🟢 Healthy" if success_rate > 95 else "🟡 Warning" if success_rate > 80 else "🔴 Critical"
        }

    def print_dashboard(self):
        """Print beautiful dashboard to terminal"""
        stats = self.get_stats()
        revenue = self.get_revenue_estimate()
        health = self.get_pipeline_health()

        print("\n" + "=" * 80)
        print(" " * 20 + "🎬 YOUTUBE AUTOMATION DASHBOARD")
        print("=" * 80)

        # Main metrics
        print(f"\n📊 MAIN METRICS")
        print(f"{'─' * 80}")
        print(f"  Total Videos Generated:  {stats['total_videos']}")
        print(f"  Uploaded to YouTube:     {stats['uploaded_to_youtube']}")
        print(f"  Total Views:             {stats['total_views']:,}")
        print(f"  Average Views/Video:     {stats['avg_views']:,}")
        print(f"  Failed Videos:           {stats['failed_videos']}")

        # Recent activity
        print(f"\n📈 RECENT ACTIVITY")
        print(f"{'─' * 80}")
        print(f"  Videos Generated Today:  {stats['videos_today']}")
        print(f"  Videos This Week:        {stats['videos_this_week']}")

        # Status breakdown
        print(f"\n🔄 STATUS BREAKDOWN")
        print(f"{'─' * 80}")
        for status, count in stats['status_breakdown'].items():
            pct = (count / stats['total_videos'] * 100) if stats['total_videos'] > 0 else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            print(f"  {status:20} {count:3} ({pct:5.1f}%) {bar}")

        # Revenue
        print(f"\n💰 REVENUE ESTIMATE")
        print(f"{'─' * 80}")
        print(f"  Total Views:             {revenue['total_views']:,}")
        print(f"  CPM Range:               {revenue['cpm_range']}")
        print(f"  Conservative:            ${revenue['revenue']['conservative']:.2f}")
        print(f"  Moderate:                ${revenue['revenue']['moderate']:.2f}")
        print(f"  Optimistic:              ${revenue['revenue']['optimistic']:.2f}")

        # Health check
        print(f"\n🏥 PIPELINE HEALTH")
        print(f"{'─' * 80}")
        print(f"  Status:                  {health['health_status']}")
        print(f"  Success Rate:            {health['success_rate']}")
        print(f"  Avg Generation Time:     {health['avg_generation_time_minutes']} minutes")
        print(f"  Recent Failures (7d):    {health['recent_failures_7d']}")

        # Recent videos
        print(f"\n📹 RECENT VIDEOS")
        print(f"{'─' * 80}")
        videos = self.get_all_videos()[:5]

        if videos:
            for video_id, title, status, yt_id, views, created in videos:
                status_emoji = {
                    "published": "✅",
                    "uploaded": "✅",
                    "video_ready": "⏳",
                    "script_generated": "🔄",
                    "failed": "❌",
                    "queued": "⏱️"
                }.get(status, "❓")

                print(f"  {status_emoji} {title[:50]}")
                print(f"     Status: {status} | Views: {views or 'N/A'} | {created[:10]}")
        else:
            print("  No videos generated yet")

        # Recent logs
        print(f"\n📋 RECENT ACTIVITY LOG")
        print(f"{'─' * 80}")
        logs = self.get_recent_logs(5)

        if logs:
            for video_id, step, status, message, timestamp in logs:
                print(f"  [{timestamp}] {step}: {status}")
                if message:
                    print(f"    └─ {message[:60]}")
        else:
            print("  No activity yet")

        print("\n" + "=" * 80 + "\n")

    def export_csv(self, filename="youtube_videos.csv"):
        """Export all videos to CSV"""
        import csv

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, title, topic, status, youtube_video_id, views,
                   created_at, updated_at, error_message
            FROM youtube_videos
            ORDER BY created_at DESC
        """)

        rows = cursor.fetchall()
        conn.close()

        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Video ID', 'Title', 'Topic', 'Status', 'YouTube ID',
                'Views', 'Created', 'Updated', 'Error'
            ])
            writer.writerows(rows)

        log.info(f"✅ Exported {len(rows)} videos to {filename}")
        return filename

    def get_top_videos(self, limit=10):
        """Get top performing videos"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT title, views, youtube_video_id, created_at
            FROM youtube_videos
            WHERE youtube_video_id IS NOT NULL
            ORDER BY views DESC
            LIMIT ?
        """, (limit,))

        videos = cursor.fetchall()
        conn.close()

        return videos

    def print_top_videos(self):
        """Print top performing videos"""
        videos = self.get_top_videos()

        print("\n" + "=" * 80)
        print(" " * 25 + "🏆 TOP PERFORMING VIDEOS")
        print("=" * 80)

        if videos:
            for i, (title, views, yt_id, created) in enumerate(videos, 1):
                print(f"\n  {i}. {title}")
                print(f"     Views: {views:,} | YouTube: youtube.com/watch?v={yt_id}")
                print(f"     Created: {created}")
        else:
            print("\n  No videos with views yet")

        print("\n" + "=" * 80 + "\n")


def main():
    """Run dashboard"""
    import sys

    dashboard = YouTubeDashboard()

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "show":
            dashboard.print_dashboard()

        elif command == "top":
            dashboard.print_top_videos()

        elif command == "export":
            filename = sys.argv[2] if len(sys.argv) > 2 else "youtube_videos.csv"
            dashboard.export_csv(filename)

        elif command == "watch":
            # Continuous monitoring
            import time
            while True:
                print("\x1b[2J\x1b[H")  # Clear screen
                dashboard.print_dashboard()
                time.sleep(30)  # Refresh every 30 seconds

        else:
            print(f"Unknown command: {command}")
            print("\nAvailable commands:")
            print("  python youtube_dashboard.py show        - Show dashboard")
            print("  python youtube_dashboard.py top         - Show top videos")
            print("  python youtube_dashboard.py export      - Export to CSV")
            print("  python youtube_dashboard.py watch       - Live monitoring")

    else:
        dashboard.print_dashboard()


if __name__ == "__main__":
    main()
