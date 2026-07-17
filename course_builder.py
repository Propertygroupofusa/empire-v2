"""
Course Builder - Bundle videos into courses and manage student access
Video-based education content with progress tracking
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from enum import Enum

import stripe

from payments_pause import payments_paused, PAUSE_MESSAGE

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("course_builder")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


class CourseLevel(str, Enum):
    """Course difficulty levels"""
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class CourseModule:
    """A module within a course"""

    def __init__(self, module_id: str, title: str, description: str, order: int):
        self.module_id = module_id
        self.title = title
        self.description = description
        self.order = order
        self.lessons: List[Dict] = []
        self.created_at = datetime.utcnow()

    def add_lesson(self, video_id: str, lesson_title: str, duration: int, content: str):
        """Add a video lesson to the module"""
        lesson = {
            "lesson_id": f"LESSON-{uuid.uuid4().hex[:8].upper()}",
            "video_id": video_id,
            "title": lesson_title,
            "duration": duration,  # in seconds
            "content": content,  # Transcript or notes
            "order": len(self.lessons) + 1
        }
        self.lessons.append(lesson)
        return lesson["lesson_id"]

    def to_dict(self) -> Dict:
        return {
            "module_id": self.module_id,
            "title": self.title,
            "description": self.description,
            "order": self.order,
            "lesson_count": len(self.lessons),
            "total_duration": sum(l["duration"] for l in self.lessons),
            "lessons": self.lessons
        }


class Course:
    """Represents an online course"""

    def __init__(self, course_id: str, title: str, description: str, level: CourseLevel, price: float):
        self.course_id = course_id
        self.title = title
        self.description = description
        self.level = level
        self.price = price  # in dollars
        self.modules: Dict[str, CourseModule] = {}
        self.created_at = datetime.utcnow()
        self.published = False
        self.enrolled_students = set()
        self.reviews = []

    def add_module(self, title: str, description: str, order: Optional[int] = None) -> str:
        """Add a module to the course"""
        module_id = f"MOD-{uuid.uuid4().hex[:8].upper()}"
        if order is None:
            order = len(self.modules) + 1

        module = CourseModule(module_id, title, description, order)
        self.modules[module_id] = module
        return module_id

    def publish(self) -> bool:
        """Publish the course"""
        if len(self.modules) == 0:
            log.warning(f"Cannot publish course {self.course_id}: no modules")
            return False

        for module in self.modules.values():
            if len(module.lessons) == 0:
                log.warning(f"Cannot publish course {self.course_id}: module {module.module_id} has no lessons")
                return False

        self.published = True
        log.info(f"Course published: {self.course_id}")
        return True

    def add_review(self, student_id: str, rating: int, comment: str):
        """Add a student review"""
        if rating < 1 or rating > 5:
            return False

        self.reviews.append({
            "student_id": student_id,
            "rating": rating,
            "comment": comment,
            "created_at": datetime.utcnow().isoformat()
        })
        return True

    def get_average_rating(self) -> float:
        """Get average course rating"""
        if not self.reviews:
            return 0.0
        return sum(r["rating"] for r in self.reviews) / len(self.reviews)

    def to_dict(self) -> Dict:
        return {
            "course_id": self.course_id,
            "title": self.title,
            "description": self.description,
            "level": self.level,
            "price": self.price,
            "published": self.published,
            "modules": {mid: m.to_dict() for mid, m in self.modules.items()},
            "total_lessons": sum(len(m.lessons) for m in self.modules.values()),
            "total_duration": sum(
                sum(l["duration"] for l in m.lessons)
                for m in self.modules.values()
            ),
            "enrolled_students": len(self.enrolled_students),
            "average_rating": self.get_average_rating(),
            "review_count": len(self.reviews),
            "created_at": self.created_at.isoformat()
        }


class StudentProgress:
    """Track a student's progress in a course"""

    def __init__(self, student_id: str, course_id: str):
        self.student_id = student_id
        self.course_id = course_id
        self.enrolled_at = datetime.utcnow()
        self.lessons_completed: Dict[str, datetime] = {}
        self.last_accessed = datetime.utcnow()

    def mark_lesson_complete(self, lesson_id: str):
        """Mark a lesson as completed"""
        self.lessons_completed[lesson_id] = datetime.utcnow()
        self.last_accessed = datetime.utcnow()

    def get_completion_percentage(self, total_lessons: int) -> float:
        """Get course completion percentage"""
        if total_lessons == 0:
            return 0.0
        return (len(self.lessons_completed) / total_lessons) * 100

    def is_course_complete(self, total_lessons: int) -> bool:
        """Check if course is fully completed"""
        return len(self.lessons_completed) == total_lessons

    def to_dict(self) -> Dict:
        return {
            "student_id": self.student_id,
            "course_id": self.course_id,
            "enrolled_at": self.enrolled_at.isoformat(),
            "lessons_completed": len(self.lessons_completed),
            "last_accessed": self.last_accessed.isoformat()
        }


class CourseBuilder:
    """Manage courses and student enrollment"""

    def __init__(self):
        self.courses: Dict[str, Course] = {}
        self.student_progress: Dict[str, Dict[str, StudentProgress]] = {}  # {student_id: {course_id: progress}}
        self.stripe_enabled = bool(os.getenv("STRIPE_SECRET_KEY"))

    def create_course(self, title: str, description: str, level: CourseLevel, price: float) -> Dict:
        """Create a new course"""
        try:
            course_id = f"COURSE-{uuid.uuid4().hex[:8].upper()}"
            course = Course(course_id, title, description, level, price)
            self.courses[course_id] = course

            log.info(f"Course created: {course_id} ({title})")

            return {
                "success": True,
                "course_id": course_id,
                "message": f"Course '{title}' created"
            }
        except Exception as e:
            log.error(f"Failed to create course: {e}")
            return {"success": False, "error": str(e)}

    def enroll_student(self, student_id: str, student_email: str, course_id: str) -> Dict:
        """Enroll a student in a course"""
        try:
            if course_id not in self.courses:
                return {"error": "Course not found"}

            course = self.courses[course_id]

            if course.price > 0 and self.stripe_enabled and payments_paused():
                return {"success": False, "error": PAUSE_MESSAGE}

            # Process payment
            if course.price > 0 and self.stripe_enabled:
                payment = stripe.PaymentIntent.create(
                    amount=int(course.price * 100),
                    currency="usd",
                    metadata={
                        "student_id": student_id,
                        "student_email": student_email,
                        "course_id": course_id
                    }
                )

                # In production, wait for payment confirmation
                # For now, assume payment succeeds
                log.info(f"Payment intent created for enrollment: {payment.id}")

            # Enroll student
            course.enrolled_students.add(student_id)

            if student_id not in self.student_progress:
                self.student_progress[student_id] = {}

            progress = StudentProgress(student_id, course_id)
            self.student_progress[student_id][course_id] = progress

            return {
                "success": True,
                "student_id": student_id,
                "course_id": course_id,
                "message": f"Successfully enrolled in '{course.title}'"
            }
        except Exception as e:
            log.error(f"Failed to enroll student: {e}")
            return {"success": False, "error": str(e)}

    def mark_lesson_complete(self, student_id: str, course_id: str, lesson_id: str) -> Dict:
        """Mark a lesson as completed by a student"""
        if student_id not in self.student_progress or course_id not in self.student_progress[student_id]:
            return {"error": "Student not enrolled in course"}

        progress = self.student_progress[student_id][course_id]
        progress.mark_lesson_complete(lesson_id)

        return {
            "success": True,
            "student_id": student_id,
            "course_id": course_id,
            "lesson_id": lesson_id,
            "progress": progress.to_dict()
        }

    def get_student_progress(self, student_id: str, course_id: str) -> Optional[Dict]:
        """Get student progress in a course"""
        if student_id not in self.student_progress or course_id not in self.student_progress[student_id]:
            return None

        progress = self.student_progress[student_id][course_id]
        course = self.courses.get(course_id)

        if not course:
            return None

        total_lessons = sum(len(m.lessons) for m in course.modules.values())

        return {
            **progress.to_dict(),
            "completion_percentage": progress.get_completion_percentage(total_lessons),
            "is_complete": progress.is_course_complete(total_lessons)
        }

    def get_course_stats(self, course_id: str) -> Dict:
        """Get statistics for a course"""
        if course_id not in self.courses:
            return {"error": "Course not found"}

        course = self.courses[course_id]

        # Calculate average completion
        total_students = len(course.enrolled_students)
        if total_students == 0:
            avg_completion = 0
        else:
            total_lessons = sum(len(m.lessons) for m in course.modules.values())
            completions = [
                len(self.student_progress[sid][course_id].lessons_completed)
                for sid in course.enrolled_students
                if sid in self.student_progress and course_id in self.student_progress[sid]
            ]
            avg_completion = sum(completions) / total_lessons if total_lessons > 0 else 0

        # Calculate revenue
        total_revenue = course.price * total_students

        return {
            "course_id": course_id,
            "title": course.title,
            "enrolled_students": total_students,
            "total_revenue": total_revenue,
            "avg_completion_percentage": avg_completion * 100,
            "average_rating": course.get_average_rating(),
            "reviews": len(course.reviews)
        }

    def get_courses(self, published_only: bool = True) -> List[Dict]:
        """Get all courses"""
        courses = list(self.courses.values())

        if published_only:
            courses = [c for c in courses if c.published]

        return [c.to_dict() for c in courses]

    def get_course_catalog(self) -> Dict:
        """Get course catalog for marketing"""
        courses = self.get_courses(published_only=True)

        total_students = sum(c["enrolled_students"] for c in courses)
        total_revenue = sum(c["enrolled_students"] * self.courses[c["course_id"]].price for c in courses)

        return {
            "total_courses": len(courses),
            "total_students": total_students,
            "total_revenue": total_revenue,
            "courses": courses
        }


# Global instance
builder = CourseBuilder()


def get_builder():
    """Get builder instance"""
    return builder
