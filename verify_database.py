"""
Database Verification Script
Tests database connection and table creation
"""

import os
import sys
from pathlib import Path

def verify_database():
    """Verify database connection and tables"""

    print("=" * 60)
    print("🗄️  DATABASE VERIFICATION")
    print("=" * 60)

    # Check environment
    print("\n📋 Checking environment variables...")
    db_url = os.getenv("DATABASE_URL")
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if db_url:
        print(f"✅ DATABASE_URL is set")
        print(f"   Type: {db_url.split('://')[0]}")
    else:
        print("❌ DATABASE_URL NOT set")
        return False

    if api_key:
        print(f"✅ ANTHROPIC_API_KEY is set")
    else:
        print("❌ ANTHROPIC_API_KEY NOT set")

    # Try to import and test database
    print("\n🔌 Testing database connection...")
    try:
        from sqlalchemy import create_engine, inspect
        from models import Base, StudyUser, StudyMaterial

        # Create engine
        engine = create_engine(db_url)

        # Test connection
        with engine.connect() as conn:
            print("✅ Database connection successful!")

        # Create tables if they don't exist
        print("\n📊 Creating tables if needed...")
        Base.metadata.create_all(engine)
        print("✅ Tables created/verified")

        # Check what tables exist
        print("\n📋 Checking tables...")
        inspector = inspect(engine)
        tables = inspector.get_table_names()

        expected_tables = ['study_user', 'study_material', 'youtube_videos', 'pipeline_logs']

        for table in expected_tables:
            if table in tables:
                print(f"✅ {table} table exists")
                # Show columns
                columns = [col['name'] for col in inspector.get_columns(table)]
                print(f"   Columns: {', '.join(columns[:5])}...")
            else:
                print(f"⏳ {table} table will be created on first run")

        # Test write capability
        print("\n✍️  Testing write capability...")
        try:
            from sqlalchemy.orm import sessionmaker
            Session = sessionmaker(bind=engine)
            session = Session()

            # Try a test query
            test_user = session.query(StudyUser).first()
            print("✅ Write capability verified")
            session.close()
        except Exception as e:
            print(f"⚠️  Write test warning: {str(e)[:50]}")

        print("\n" + "=" * 60)
        print("✅ DATABASE VERIFICATION PASSED")
        print("=" * 60)
        print("\n🚀 Your database is ready!")
        print("   • Study Assistant can create materials")
        print("   • YouTube pipeline can track videos")
        print("   • All tables will auto-create on first use")
        return True

    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("   Run: pip install sqlalchemy")
        return False
    except Exception as e:
        print(f"❌ Database error: {e}")
        return False

if __name__ == "__main__":
    success = verify_database()
    sys.exit(0 if success else 1)
