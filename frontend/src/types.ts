export type Role = 'STUDENT' | 'TEACHER';
export type AssessmentKind = 'LAB' | 'INDEPENDENT' | 'CONTROL' | 'EXAM';
export type AssessmentStatus = 'UPCOMING' | 'AVAILABLE' | 'IN_PROGRESS' | 'SUBMITTED' | 'GRADED' | 'CLOSED';

export interface Membership {
  courseId: string;
  courseName: string;
  role: Role;
  group?: string;
}

export interface Session {
  id: string;
  displayName: string;
  providerName: string;
  memberships: Membership[];
  capabilities: string[];
  adminElevationExpiresAt?: string;
}

export interface AuthConnection {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  loginMode: 'CREDENTIALS' | 'REDIRECT';
}

export interface LmsSyncDiagnostic {
  code: string;
  message: string;
  at?: string;
  retryable: boolean;
}

export interface Course {
  id: string;
  title: string;
  shortName: string;
  role: Role;
  group?: string;
  term: string;
  provider: string;
  externalUrl?: string;
  syncStatus: 'SYNCED' | 'SYNCING' | 'STALE' | 'ERROR';
  syncError?: LmsSyncDiagnostic;
  syncedAt?: string;
  activeCount?: number;
  uncheckedCount?: number;
}

export interface CourseSyncStatus {
  courseId: string;
  syncStatus: Course['syncStatus'];
  updatedAt?: string;
  syncError?: LmsSyncDiagnostic;
}

export interface CourseCatalogEntry {
  id: string;
  connectionId: string;
  connectionName: string;
  externalId: string;
  title: string;
  shortName: string;
  externalUrl: string;
  syncStatus: 'SYNCED' | 'SYNCING' | 'STALE' | 'ERROR';
  syncError?: LmsSyncDiagnostic;
  addedAt?: string;
}

export interface TeacherAccessToken {
  id: string;
  label: string;
  publicId: string;
  hashFingerprint: string;
  canReveal: boolean;
  boundPrincipalId?: string;
  boundDisplayName?: string;
  useCount: number;
  lastUsedAt?: string;
  createdAt: string;
}

export interface TeacherAccessTokenIssued extends TeacherAccessToken {
  token: string;
}

export interface LmsActivity {
  cmid: number;
  instance_id: number;
  module: string;
  name: string;
  visible: boolean;
  user_visible: boolean;
  section_external_id: string;
  url?: string;
  opens_at_epoch: number;
  due_at_epoch: number;
  cutoff_at_epoch: number;
  grade_max?: number;
}

export interface CourseGroup {
  id: string;
  courseId: string;
  externalId: string;
  name: string;
  kind: string;
  active: boolean;
}

export interface AvailabilityRule {
  id: string;
  targetType: 'COURSE' | 'GROUP' | 'PRINCIPAL';
  targetExternalId: string;
  allowed: boolean;
  opensAt?: string;
  closesAt?: string;
  durationSeconds?: number;
  attemptLimit?: number;
}

export interface AssessmentPublicationGroup {
  id: string;
  externalId: string;
  name: string;
}

export interface AssessmentPublicationPrincipal {
  principalId: string;
  externalSubject: string;
  displayName: string;
  groups: string[];
  opensAt?: string;
  closesAt?: string;
  durationSeconds?: number;
  attemptLimit?: number;
  attemptsUnlimited: boolean;
}

export interface AssessmentPublicationTargets {
  groups: AssessmentPublicationGroup[];
  principals: AssessmentPublicationPrincipal[];
  overridesConfirmed: boolean;
}

export interface Assessment {
  id: string;
  courseId: string;
  title: string;
  summary: string;
  kind: AssessmentKind;
  status: AssessmentStatus;
  startsAt?: string;
  deadlineAt?: string;
  durationMinutes?: number;
  attemptLimit?: number;
  attemptId?: string;
  progress?: number;
  score?: number;
  maxScore: number;
  fileMode: 'SINGLE' | 'MULTI';
  language: 'C' | 'CPP';
  standard: string;
  pastePolicy: 'STRICT' | 'ALLOW';
  aiEnabled: boolean;
  reviewRequired?: boolean;
  decisionSupportEnabled?: boolean;
  availabilityRules?: AvailabilityRule[];
  submissionsCount?: number;
  uncheckedCount?: number;
  publicationStatus?: 'DRAFT' | 'PUBLISHED' | 'CLOSED';
  taskVersionIds?: string[];
  policy?: Record<string, unknown>;
  requiresLiveLmsPreparation?: boolean;
}

export interface WorkspaceFile {
  id: string;
  path: string;
  content: string;
  readOnly?: boolean;
  language?: string;
}

export interface Attempt {
  id: string;
  assessmentId: string;
  title: string;
  statement: string;
  status: 'ACTIVE' | 'SUBMITTED' | 'LOCKED';
  revision: number;
  acknowledgedRevision: number;
  startedAt: string;
  expectedEndAt?: string;
  deadlineAt?: string;
  closureReason?: string;
  closedAt?: string;
  lastCheckpointAt?: string;
  checkpointStatus: 'SYNCED' | 'PENDING' | 'ERROR';
  pastePolicy: 'STRICT' | 'ALLOW';
  aiEnabled: boolean;
  fileMode: 'SINGLE' | 'MULTI';
  files: WorkspaceFile[];
  requiresLiveLmsPreparation?: boolean;
}

export interface AttemptStatus {
  id: string;
  status: Attempt['status'];
  closureReason?: string;
  closedAt?: string;
  lastCheckpointAt?: string;
  checkpointStatus: Attempt['checkpointStatus'];
}

export interface Diagnostic {
  id: string;
  fileId?: string;
  path?: string;
  line?: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
  severity: 'error' | 'warning' | 'info';
  code?: string;
  message: string;
  notes?: string[];
}

export interface RunResult {
  id: string;
  state: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
  revision: number;
  exitCode?: number;
  stdout: string;
  stderr: string;
  durationMs?: number;
  memoryKb?: number;
  diagnostics: Diagnostic[];
  createdAt: string;
}

export interface InteractiveRun {
  sessionId: string;
  status: 'RUNNING' | 'SUCCESS' | 'COMPILE_ERROR' | 'RUNTIME_ERROR' | 'TIME_LIMIT' | 'MEMORY_LIMIT' | 'OUTPUT_LIMIT' | 'WORKSPACE_LIMIT' | 'STOPPED' | 'INFRA_ERROR';
  terminal: boolean;
  exitCode?: number;
  durationMs: number;
  stdout: string;
  stderr: string;
  outputTruncated: boolean;
  inputClosed?: boolean;
  diagnostics: Diagnostic[];
}

export interface ClientContext {
  ipAddress?: string;
  browser?: string;
  browserVersion?: string;
  operatingSystem?: string;
  deviceType?: 'DESKTOP' | 'MOBILE' | 'TABLET' | 'BOT' | string;
}

export interface HistoryEvent {
  id: string;
  type: 'edit' | 'run' | 'snapshot' | 'paste_blocked' | 'internal_paste' | 'submit';
  label: string;
  detail?: string;
  at: string;
  revision: number;
  client?: ClientContext;
}

export interface ReviewDecisionSummary {
  id?: string;
  reviewerName: string;
  grade: number;
  comment: string;
  revision: number;
  reviewedAt: string;
  lmsExportState: string;
}

export type SubmissionStatus = 'UNGRADED' | 'CLAIMED' | 'GRADED' | 'CONFLICT';

/**
 * One independently reviewable question inside a single LMS response.
 *
 * Moodle Quiz stores every question as a separate submission in the review
 * API, while the teacher perceives them as one student's answer.  The stable
 * submission id is therefore the navigation target; position and presentation
 * fields let the review UI switch between questions without guessing from
 * titles or ids.
 */
export interface SubmissionReviewGroupItem {
  submissionId: string;
  position: number;
  title: string;
  score?: number;
  maxScore?: number;
  status?: SubmissionStatus;
}

export interface SubmissionReviewGroup {
  id: string;
  title?: string;
  items: SubmissionReviewGroupItem[];
}

export interface Submission {
  id: string;
  source?: string;
  assessmentId: string;
  taskVersionId?: string;
  courseId?: string;
  courseTitle?: string;
  assessmentTitle: string;
  studentName: string;
  studentGroup: string;
  submittedAt: string;
  status: SubmissionStatus;
  score?: number;
  maxScore: number;
  claim?: {
    id: string;
    ownerId: string;
    ownerName: string;
    expiresAt: string;
    mine: boolean;
  };
  risk: 'LOW' | 'MEDIUM' | 'HIGH' | 'UNKNOWN';
  testsPassed: number;
  testsTotal: number;
  reviewRequired?: boolean;
  decisionSupportEnabled?: boolean;
  canReview?: boolean;
  files: WorkspaceFile[];
  history: HistoryEvent[];
  latestDecision?: ReviewDecisionSummary;
  decisionHistory: ReviewDecisionSummary[];
  reviewGroup?: SubmissionReviewGroup;
  originVerification?: {
    state: 'VERIFIED' | 'EXTERNAL_ORIGIN' | 'MISMATCH' | 'PENDING' | 'UNAVAILABLE';
    transport?: string;
    checkedAt?: string;
    message: string;
  };
}

export interface TeacherExperiment {
  id: string;
  submissionId: string;
  revision: number;
  files: WorkspaceFile[];
  changed: boolean;
  createdAt: string;
}

export interface AuthorshipAnalysisResult {
  manifestHash?: string;
  probability?: number;
  confidence?: number;
  uncertainty?: number;
  analyzer?: string;
  model?: string;
  calibration: Record<string, unknown>;
  features: Record<string, unknown>;
  warnings: string[];
  responseHash?: string;
  createdAt?: string;
}

export interface AuthorshipAnalysis {
  id: string;
  submissionId: string;
  manifestHash?: string;
  payloadHash?: string;
  exportSchemaVersion?: string;
  state: string;
  outcome?: string;
  probability?: number;
  startedAt?: string;
  completedAt?: string;
  errorCode?: string;
  error?: string;
  result?: AuthorshipAnalysisResult;
}

export interface SimilarityMatch {
  id: string;
  submissionA: string;
  submissionB: string;
  score: number;
  fingerprintCountA: number;
  fingerprintCountB: number;
  sharedFingerprintCount: number;
  evidence: SimilarityEvidenceFragment[];
  case?: Record<string, unknown>;
}

export interface SimilarityEvidenceFragment {
  fileA: string;
  startLineA: number;
  endLineA: number;
  fileB: string;
  startLineB: number;
  endLineB: number;
  tokenCount: number;
  excerptA: string;
  excerptB: string;
}

export interface SimilaritySubmissionSide {
  submissionId: string;
  studentName: string;
  studentGroup: string;
  submittedAt: string;
  files: WorkspaceFile[];
}

export interface SimilarityComparison {
  match: SimilarityMatch;
  assessmentId: string;
  assessmentTitle: string;
  left: SimilaritySubmissionSide;
  right: SimilaritySubmissionSide;
}

export interface SimilarityAnalysis {
  id: string;
  assessmentId: string;
  taskVersionId?: string;
  algorithmVersion?: string;
  config: Record<string, unknown>;
  state: string;
  submissionCount: number;
  comparisonCount: number;
  matchCount: number;
  startedAt?: string;
  completedAt?: string;
  error?: string;
  matches: SimilarityMatch[];
}

export interface SystemSettings {
  revision?: number;
  aiEnabled: boolean;
  studentAiEnabled: boolean;
  runnerEnabled: boolean;
  runnerCpuSeconds: number;
  runnerMemoryMb: number;
  retentionDays: number;
  allowedLmsOrigins: string[];
  incidentBanner: string;
  services: Array<{ name: string; status: 'ok' | 'degraded' | 'down'; detail: string }>;
}

export interface SystemHealth {
  status: 'ok' | 'degraded' | 'down';
  database: 'ok' | 'error' | string;
  build: string;
}

export interface CourseImportJob {
  id: string;
  state: string;
  externalCourseId: string;
  preview: Record<string, unknown>;
  capabilityReport: Record<string, unknown>;
  confirmedCourse?: string;
  error?: string;
}

export interface MoodleHistoryImportEvent {
  id: string;
  courseId?: string;
  aggregateId: string;
  actorKey?: string;
  state: 'PENDING' | 'PROCESSING' | 'RETRY' | 'DELIVERED' | 'FAILED' | 'BLOCKED';
  createdAt: string;
  updatedAt: string;
  receipt: Record<string, unknown>;
}

export interface TaskBankItem {
  id: string;
  course?: string;
  scope: 'COURSE' | 'SYSTEM';
  slug: string;
  category: string;
  tags: string[];
  createdAt?: string;
  latestVersion?: TaskVersion;
}

export interface TaskVersion {
  id: string;
  itemId: string;
  number: number;
  title: string;
  statement: string;
  language: string;
  languageStandard: string;
  multiFile: boolean;
  maxScore: number;
  status: string;
  hiddenTestManifest?: HiddenTestManifestV1;
  aiPolicy?: Record<string, unknown>;
}

export type HiddenTestComparison = 'EXACT' | 'TRIM_TRAILING_WHITESPACE';

export interface HiddenTestCase {
  name: string;
  stdin: string;
  expected_stdout: string;
  comparison: HiddenTestComparison;
}

export interface HiddenTestManifestV1 {
  schema_version: 1;
  cases: HiddenTestCase[];
}

export interface EvidenceCaseOutcome {
  caseIndex: number;
  name: string;
  runId: string;
  status: 'PASSED' | 'FAILED' | 'INFRASTRUCTURE_ERROR';
  comparison: HiddenTestComparison;
  exitCode?: number;
  actualStdoutSha256: string;
  expectedStdoutSha256: string;
  actualStdoutPreview: string;
  stderrPreview: string;
  filesystemIsolated?: boolean;
  networkEnabled?: boolean;
}

export interface EvidenceFinding {
  code: string;
  message: string;
  caseIndex?: number;
  runId?: string;
}

export interface EvidenceReport {
  id: string;
  submissionId: string;
  snapshotId: string;
  taskVersionId: string;
  requestedById: string;
  hiddenTestManifestHash: string;
  taskContentHash: string;
  snapshotManifestHash: string;
  status: 'RUNNING' | 'COMPLETED' | 'FAILED';
  passedCases: number;
  totalCases: number;
  outcomes: EvidenceCaseOutcome[];
  findings: EvidenceFinding[];
  failureCode?: string;
  failureMessage?: string;
  completedAt?: string;
  createdAt: string;
  updatedAt: string;
}

export interface ApiErrorBody {
  code?: string;
  message?: string;
  detail?: string;
  fields?: Record<string, string[]>;
  trace_id?: string;
}
