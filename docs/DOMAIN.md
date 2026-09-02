# Ragra Domain Model

## Task
A task originates from a Classroom source or is manually created.

Important fields:
- source ID
- course
- title
- description
- actual_deadline
- personal_deadline
- status
- created_at
- completed_at

## Deadline semantics
`actual_deadline` is authoritative academic information.
`personal_deadline` is the user's intended completion time.

They are never interchangeable.

## States
Use only states needed by the implementation. A reasonable starting set:
DISCOVERED, ACTION_REQUIRED, PLANNED, IN_PROGRESS, COMPLETED, MISSED, CANCELLED, ARCHIVED.

## History
Deadline changes, completion, missed status, cancellation, and important source changes should be auditable.

## Timetable
Represent:
course, instructor, room, day, start, end, section, status.

## Source identity
Use stable external IDs for Classroom and timetable records. Titles are not identities.
