CREATE TABLE alerts (
	id UUID NOT NULL, 
	client_id VARCHAR(100) NOT NULL, 
	source VARCHAR(100) NOT NULL, 
	idempotency_key VARCHAR(200), 
	request_hash VARCHAR(64) NOT NULL, 
	fingerprint VARCHAR(128) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	duplicate_of UUID, 
	payload JSONB NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_alerts_client_idempotency_key UNIQUE (client_id, idempotency_key), 
	CONSTRAINT ck_alerts_status CHECK (status IN ('ACCEPTED','DUPLICATE_SUPPRESSED'))
);

CREATE INDEX ix_alerts_fingerprint_received ON alerts (fingerprint, received_at);

CREATE INDEX ix_alerts_received ON alerts (received_at);

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
