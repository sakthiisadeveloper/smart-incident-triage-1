-- One PostgreSQL server for local development, but one database and one login
-- per service: no service can read another service's tables.
-- (In Azure each service gets its own database; credentials come from Key Vault
-- or Entra ID managed identities instead of these local passwords.)

CREATE USER ingestion_svc WITH PASSWORD 'ingestion_pw';
CREATE DATABASE ingestion OWNER ingestion_svc;
REVOKE ALL ON DATABASE ingestion FROM PUBLIC;

CREATE USER incident_svc WITH PASSWORD 'incident_pw';
CREATE DATABASE incidents OWNER incident_svc;
REVOKE ALL ON DATABASE incidents FROM PUBLIC;
