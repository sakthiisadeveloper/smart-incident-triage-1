CREATE TABLE incidents (
	id BIGSERIAL NOT NULL, 
	source VARCHAR(100) NOT NULL, 
	environment VARCHAR(20) NOT NULL, 
	fingerprint VARCHAR(128) NOT NULL, 
	title VARCHAR(300) NOT NULL, 
	last_message TEXT NOT NULL, 
	tags JSONB NOT NULL, 
	severity VARCHAR(2) NOT NULL, 
	category VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	assignee VARCHAR(200), 
	sla_due_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	alert_count INTEGER NOT NULL, 
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	resolution_note TEXT, 
	version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_incidents_status CHECK (status IN ('OPEN','ACKNOWLEDGED','IN_PROGRESS','RESOLVED')), 
	CONSTRAINT ck_incidents_severity CHECK (severity IN ('P1','P2','P3','P4')), 
	CONSTRAINT ck_incidents_alert_count CHECK (alert_count >= 1)
);

CREATE INDEX ix_incidents_category_created ON incidents (category, created_at);

CREATE INDEX ix_incidents_created ON incidents (created_at);

CREATE INDEX ix_incidents_severity_created ON incidents (severity, created_at);

CREATE INDEX ix_incidents_source_created ON incidents (source, created_at);

CREATE INDEX ix_incidents_status_created ON incidents (status, created_at);

CREATE UNIQUE INDEX uq_incidents_open_fingerprint ON incidents (fingerprint) WHERE status <> 'RESOLVED';

CREATE TABLE outbox (
	id BIGSERIAL NOT NULL, 
	event_id UUID NOT NULL, 
	event_type VARCHAR(100) NOT NULL, 
	routing_key VARCHAR(200) NOT NULL, 
	envelope JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE, 
	attempts INTEGER NOT NULL, 
	last_error TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (event_id)
);

CREATE INDEX ix_outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE processed_events (
	consumer VARCHAR(100) NOT NULL, 
	event_id UUID NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_processed_events PRIMARY KEY (consumer, event_id)
);

CREATE TABLE agent_suggestions (
	id BIGSERIAL NOT NULL, 
	incident_id BIGINT NOT NULL, 
	source_event_id UUID NOT NULL, 
	outcome VARCHAR(20) NOT NULL, 
	fallback_reason VARCHAR(50), 
	category VARCHAR(50) NOT NULL, 
	severity VARCHAR(2) NOT NULL, 
	confidence FLOAT NOT NULL, 
	summary TEXT NOT NULL, 
	proposed_actions JSONB NOT NULL, 
	evidence JSONB NOT NULL, 
	guardrail_flags JSONB NOT NULL, 
	model VARCHAR(100) NOT NULL, 
	prompt_version VARCHAR(50) NOT NULL, 
	decision VARCHAR(20) NOT NULL, 
	decided_by VARCHAR(200), 
	decided_at TIMESTAMP WITH TIME ZONE, 
	decision_comment TEXT, 
	steps INTEGER NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	cost_usd FLOAT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_agent_suggestions_confidence CHECK (confidence >= 0 AND confidence <= 1), 
	CONSTRAINT ck_agent_suggestions_decision CHECK (decision IN ('PENDING_REVIEW','ACCEPTED','REJECTED')), 
	CONSTRAINT ck_agent_suggestions_outcome CHECK (outcome IN ('CLASSIFIED','FALLBACK')), 
	CONSTRAINT ck_agent_suggestions_severity CHECK (severity IN ('P1','P2','P3','P4')), 
	FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE CASCADE, 
	UNIQUE (source_event_id)
);

CREATE INDEX ix_agent_suggestions_decision_created ON agent_suggestions (decision, created_at);

CREATE INDEX ix_agent_suggestions_incident_created ON agent_suggestions (incident_id, created_at);

CREATE TABLE incident_alerts (
	id BIGSERIAL NOT NULL, 
	incident_id BIGINT NOT NULL, 
	alert_id UUID NOT NULL, 
	severity_hint VARCHAR(20), 
	title VARCHAR(300) NOT NULL, 
	message TEXT NOT NULL, 
	tags JSONB NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE CASCADE, 
	UNIQUE (alert_id)
);

CREATE INDEX ix_incident_alerts_incident_received ON incident_alerts (incident_id, received_at);

CREATE TABLE incident_events (
	id BIGSERIAL NOT NULL, 
	incident_id BIGINT NOT NULL, 
	event_type VARCHAR(40) NOT NULL, 
	from_status VARCHAR(20), 
	to_status VARCHAR(20), 
	actor VARCHAR(200) NOT NULL, 
	actor_type VARCHAR(10) NOT NULL, 
	comment TEXT, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_incident_events_actor_type CHECK (actor_type IN ('SYSTEM','USER','AGENT')), 
	FOREIGN KEY(incident_id) REFERENCES incidents (id) ON DELETE RESTRICT
);

CREATE INDEX ix_incident_events_created ON incident_events (created_at);

CREATE INDEX ix_incident_events_incident_created ON incident_events (incident_id, created_at);
