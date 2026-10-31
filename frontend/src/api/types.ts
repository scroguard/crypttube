export interface Ref {
  id: number;
  name: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  total: number | null;
}

export type Role = "admin" | "editor" | "viewer";

export interface SessionUser {
  id: number;
  username: string;
  role: Role;
  can_upload: boolean;
}

export interface AuthStatus {
  setup_required: boolean;
  user: SessionUser | null;
}

export interface SceneItem {
  can_edit: boolean;
  id: number;
  title: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  release_date: string | null;
  rating: number | null;
  organized: boolean;
  play_count: number;
  resume_time: number | null;
  created_at: string;
  missing: boolean;
  studio: Ref | null;
  performers: Ref[];
  tags: Ref[];
  screenshot_url: string;
}

export interface PerformerRef extends Ref {
  disambiguation: string | null;
  gender: string | null;
  image_url: string | null;
}

export interface SceneDetail extends Omit<SceneItem, "performers" | "missing"> {
  details: string | null;
  urls: string[];
  framerate: number | null;
  bitrate: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  container: string | null;
  last_played_at: string | null;
  custom_fields: Record<string, unknown>;
  updated_at: string;
  file: { path?: string; size: number; mod_time: string; oshash: string | null; missing_since: string | null };
  studio: (Ref & { parent_id: number | null }) | null;
  performers: PerformerRef[];
  playback_url: string;
  can_edit: boolean;
  mine: boolean;
}

export type PlaybackMode = "direct" | "remux" | "transcode";

export interface Playback {
  modes: PlaybackMode[];
  direct: { url: string; mime_type: string };
  remux_url: string;
  transcode_url: string;
  transcode_heights: number[];
  video_codec: string | null;
  audio_codec: string | null;
  duration: number | null;
  screenshot_url: string;
  sprite_vtt_url: string | null;
  /** Set when the file needs special handling to play (shown under the player). */
  notice: string | null;
}

export interface GalleryItem {
  id: number;
  title: string | null;
  release_date: string | null;
  rating: number | null;
  organized: boolean;
  image_count: number;
  cover_image_id: number | null;
  created_at: string;
  studio: Ref | null;
  performers: Ref[];
  tags: Ref[];
  cover_url: string | null;
}

export interface GalleryDetail extends GalleryItem {
  details: string | null;
  folder_path: string | null;
  zip_path: string | null;
  custom_fields: Record<string, unknown>;
  can_edit: boolean;
  mine: boolean;
}

export interface ImageItem {
  can_edit: boolean;
  id: number;
  title: string | null;
  width: number | null;
  height: number | null;
  rating: number | null;
  organized: boolean;
  thumbnail_url: string;
  file_url: string;
  /** The motion of a Live Photo (a short MP4), if the image has one. */
  live_url: string | null;
}

export interface ImageDetail extends ImageItem {
  file: { path: string; zip_path: string | null; size: number; missing: boolean };
  studio: Ref | null;
  galleries: { id: number; title: string | null }[];
  performers: Ref[];
  tags: Ref[];
  can_edit: boolean;
  mine: boolean;
}

export interface PerformerItem {
  id: number;
  name: string;
  disambiguation: string | null;
  gender: string | null;
  birthdate: string | null;
  favorite: boolean;
  rating: number | null;
  image_url: string | null;
  scene_count: number;
  gallery_count: number;
  image_count: number;
}

export interface PerformerDetail extends PerformerItem {
  aliases: string[];
  tags: Ref[];
  deathdate: string | null;
  country: string | null;
  ethnicity: string | null;
  eye_color: string | null;
  hair_color: string | null;
  height_cm: number | null;
  weight_kg: number | null;
  measurements: string | null;
  tattoos: string | null;
  piercings: string | null;
  career_start: number | null;
  career_end: number | null;
  details: string | null;
  custom_fields: Record<string, unknown>;
  can_edit: boolean;
  personal: boolean;
}

export interface StudioItem {
  id: number;
  name: string;
  parent: Ref | null;
  rating: number | null;
  image_url: string | null;
  scene_count: number;
  gallery_count: number;
  child_count: number;
}

export interface StudioDetail extends Omit<StudioItem, "child_count"> {
  url: string | null;
  details: string | null;
  children: Ref[];
  image_count: number;
  can_edit: boolean;
  personal: boolean;
}

export interface TagItem {
  id: number;
  name: string;
  description: string | null;
  image_url: string | null;
  scene_count: number;
  gallery_count: number;
  image_count: number;
  child_count: number;
}

export interface TagDetail extends Omit<TagItem, "child_count"> {
  parents: Ref[];
  children: Ref[];
  performer_count: number;
  can_edit: boolean;
  personal: boolean;
}

export interface Library {
  id: number;
  path: string;
  enabled: boolean;
  watch: boolean;
  create_folder_galleries: boolean;
  exclude_patterns: string[];
  watch_state: "starting" | "active" | "error" | null;
  watch_error: string | null;
  created_at: string;
}

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Job {
  id: number;
  kind: string;
  payload: Record<string, unknown>;
  status: JobStatus;
  progress: number | null;
  message: string | null;
  error: string | null;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface AdminUser {
  id: number;
  username: string;
  role: Role;
  is_active: boolean;
  can_upload: boolean;
  upload_quota_bytes: number | null;
  created_at: string;
  last_login_at: string | null;
  restriction_count?: number;
  restrictions?: { tags: Ref[]; studios: Ref[]; performers: Ref[] };
}

export interface SessionInfo {
  id: number;
  user_agent: string | null;
  created_at: string;
  last_seen_at: string;
  current: boolean;
}

export type Visibility = "private" | "users" | "everyone";

export interface MyLibrary {
  id: number;
  name: string;
  visibility: Visibility;
  created_at: string;
  shared_with: { id: number; username: string }[];
  file_count: number;
  bytes: number;
  scene_count: number;
  image_count: number;
}

export interface MyLibraries {
  can_upload: boolean;
  quota_bytes: number | null;
  used_bytes: number;
  libraries: MyLibrary[];
  upload_extensions: string[];
}

export interface LibraryFile {
  path: string;
  size: number;
  indexed: boolean;
  scene_id: number | null;
  image_id: number | null;
}

export interface UserLibrarySummary {
  id: number;
  name: string;
  visibility: Visibility;
  created_at: string;
  owner: { id: number; username: string };
  file_count: number;
  bytes: number;
}

export interface HomeSection {
  type: string;
  limit: number;
  title: string | null;
}

export interface HomeConfig {
  sections: HomeSection[];
  available: { type: string; label: string; description: string }[];
  customized: boolean;
}
