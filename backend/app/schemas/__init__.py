from app.schemas import (
    ai as _ai,
)
from app.schemas import (
    assessments as _assessments,
)
from app.schemas import (
    attempts as _attempts,
)
from app.schemas import (
    auth as _auth,
)
from app.schemas import (
    common as _common,
)
from app.schemas import (
    courses as _courses,
)
from app.schemas import (
    integrity as _integrity,
)
from app.schemas import (
    reviews as _reviews,
)
from app.schemas import (
    runs as _runs,
)
from app.schemas import (
    system as _system,
)
from app.schemas import (
    tasks as _tasks,
)
from app.schemas.ai import *  # noqa: F403
from app.schemas.assessments import *  # noqa: F403
from app.schemas.attempts import *  # noqa: F403
from app.schemas.auth import *  # noqa: F403
from app.schemas.common import *  # noqa: F403
from app.schemas.courses import *  # noqa: F403
from app.schemas.integrity import *  # noqa: F403
from app.schemas.reviews import *  # noqa: F403
from app.schemas.runs import *  # noqa: F403
from app.schemas.system import *  # noqa: F403
from app.schemas.tasks import *  # noqa: F403

__all__ = (
    *_ai.__all__,
    *_assessments.__all__,
    *_attempts.__all__,
    *_auth.__all__,
    *_common.__all__,
    *_courses.__all__,
    *_integrity.__all__,
    *_reviews.__all__,
    *_runs.__all__,
    *_system.__all__,
    *_tasks.__all__,
)
