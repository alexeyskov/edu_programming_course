import './courseTitle.css';

/** An optional local label, never a replacement for the LMS course title. */
export function CourseTitle({ title, comment }: { title: string; comment?: string }) {
  return <>{title}{comment?.trim() && <span className="course-title__comment"> {comment}</span>}</>;
}
