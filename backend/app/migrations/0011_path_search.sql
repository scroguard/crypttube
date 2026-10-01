-- Search by title *or* file path (folders are often named after performers), each side
-- answered by its own trigram index.
CREATE INDEX files_path_trgm        ON files     USING gin (path gin_trgm_ops);
CREATE INDEX images_title_trgm      ON images    USING gin (title gin_trgm_ops);
CREATE INDEX galleries_folder_trgm  ON galleries USING gin (folder_path gin_trgm_ops);
