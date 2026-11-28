# Public Waste Collection

## MongoDB Atlas configuration

Set `MONGODB_URI` to the MongoDB Atlas connection string in the application
environment. `MONGODB_DATABASE` selects the database and defaults to
`public_waste_collection`. The older `MONGO_URI` variable is also accepted as
an alias for `MONGODB_URI`.

The WSGI entry point initializes MongoDB on startup. On its first startup, if a
legacy SQLite file exists at `LEGACY_DATABASE_PATH` (or `DATABASE_PATH`), users,
reports, and ward pincodes are imported into MongoDB. The default local source
is `database.db`; Render is configured to read `/var/data/database.db`.
Imported IDs and report/user relationships are preserved. A migration marker
prevents the SQLite file from being re-imported on later restarts.

Report photos are stored in MongoDB GridFS so they remain available across
application restarts and deployments. On startup, photos referenced by reports
are copied from the upload directory or `static/uploads` into GridFS when a
local copy still exists. The Render disk remains mounted as a source for the
one-time SQLite import and for migrating older uploaded photos.

For local development, set `MONGODB_URI` before running the app, for example
with a local MongoDB instance or an Atlas cluster. Do not commit the URI or
place it in source control.

Install `requirements-dev.txt` to run the test suite:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
