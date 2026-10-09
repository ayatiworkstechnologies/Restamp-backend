"""Initial RESTAMP migration — 28 validated tables (Phase 2 only)

Revision ID: 1a2b3c4d5e6f
Revises: 
Create Date: 2026-10-01

STOPS at DB connection (no production DB touched). Migration represents
finalized architecture from RESTAMP_DATABASE_ARCHITECTURE_V2.4.2_FINAL.md
and validated Phase 1 SQLAlchemy models (Base.metadata = 28 tables).

MySQL-specific notes (verified via offline MySQL-dialect compilation; no real DB touched):
- BIGINT UNSIGNED PKs/FKs: mysql.BIGINT(unsigned=True); compiles to BIGINT UNSIGNED
- ENUM: native MySQL ENUM via SQLAlchemy Enum
- NOT NULL / ON DELETE / ON UPDATE preserved at metadata level
- updated_at columns: TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
- Indexes: 13 required indexes declared alongside create_table

Phase 3 NOT STARTED.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import Column, Integer, String, Enum, DateTime, JSON, BigInteger, Text, ForeignKey, UniqueConstraint, Index
from sqlalchemy.dialects import mysql
from sqlalchemy.sql import func

# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e6f'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # 28-table architecture reconstructed from validated Base.metadata
    # No destructive commands executed (DB not connected — placeholder for deployment)
    # Each create_table maps to a validated Phase 1 model

    op.create_table('users',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column('avatar_url', sa.String(length=500), nullable=True),
        sa.Column('status', sa.Enum('active', 'suspended', 'closed'), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('updated_at', mysql.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('idx_users_status', 'status'),
        sa.Index('idx_users_created_at', 'created_at'),
        sa.Index('idx_users_updated_at', 'updated_at'),
    )

    op.create_table('auth_identities',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('provider', sa.Enum('phone', 'google'), nullable=False),
        sa.Column('provider_identifier', sa.String(length=500), nullable=False),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('verified_by', sa.String(length=255), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('updated_at', mysql.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'provider', name='uk_auth_identities_provider_user'),
        sa.UniqueConstraint('provider', 'provider_identifier', name='uk_auth_provider_identifier_scoped'),
    )

    op.create_table('user_current_role',
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False),
        sa.Column('assigned_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('updated_at', mysql.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('user_id'),
    )

    op.create_table('role_history',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False),
        sa.Column('changed_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('admin_accounts',
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('admin_role', sa.String(length=50), nullable=False, server_default='ADMIN'),
        sa.Column('granted_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('user_id'),
    )

    op.create_table('cities',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('districts',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('city_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('cities.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('city_id', 'name', name='uk_city_name'),
    )

    op.create_table('taluks',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('district_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('districts.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('district_id', 'name', name='uk_district_name'),
    )

    op.create_table('villages',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('taluk_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('taluks.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('taluk_id', 'name', name='uk_taluk_name'),
    )

    op.create_table('localities',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('village_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('villages.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('type', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('postcodes',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('locality_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('localities.id', ondelete='RESTRICT', onupdate='RESTRICT'), nullable=False),
        sa.Column('pincode', sa.String(length=10), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('plans',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False, unique=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('price_paise', sa.BigInteger(), nullable=False),
        sa.Column('duration_days', sa.Integer(), nullable=True),
        sa.Column('status', sa.Enum('ACTIVE', 'INACTIVE'), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('payments',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('provider_payment_id', sa.String(length=255), nullable=True),
        sa.Column('provider_order_id', sa.String(length=255), nullable=True),
        sa.Column('amount_paise', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
        sa.Column('status', sa.Enum('INITIATED', 'SUCCESS', 'FAILED'), nullable=False, server_default='INITIATED'),
        sa.Column('plan_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('plans.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('idempotency_key', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'idempotency_key', name='uk_payments_idempotency'),
        sa.UniqueConstraint('provider', 'provider_payment_id', name='uk_payments_payment_id'),
        sa.UniqueConstraint('provider', 'provider_order_id', name='uk_payments_order_id'),
        sa.Index('idx_payments_user_id', 'user_id'),
        sa.Index('idx_payments_status', 'status'),
        sa.Index('idx_payments_idempotency_key', 'idempotency_key'),
    )

    op.create_table('payment_webhook_events',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('payment_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('payments.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('provider_event_id', sa.String(length=255), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('received_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('processing_status', sa.Enum('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'), server_default='PENDING', nullable=False),
        sa.Column('processing_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'provider_event_id', name='uk_webhook_provider_event'),
        sa.Index('idx_webhook_events_payment_id', 'payment_id'),
        sa.Index('idx_webhook_events_provider_event_id', 'provider_event_id'),
    )

    op.create_table('subscriptions',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('plan_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('plans.id', ondelete='RESTRICT', onupdate='CASCADE'), nullable=False),
        sa.Column('payment_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('payments.id', ondelete='RESTRICT', onupdate='CASCADE'), nullable=False),
        sa.Column('start_date', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('end_date', sa.DateTime(), nullable=True),
        sa.Column('status', sa.Enum('ACTIVE', 'CANCELLED', 'EXPIRED'), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('entitlements',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('subscription_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('subscriptions.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=50), nullable=False),
        sa.Column('granted_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subscription_id', 'kind', name='uk_entitlement_kind'),
    )

    op.create_table('physical_properties',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('postcode_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('postcodes.id', ondelete='RESTRICT', onupdate='CASCADE'), nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('property_listings',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('physical_property_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('physical_properties.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('verification_status', sa.Enum('PENDING', 'VERIFIED'), nullable=False, server_default='PENDING'),
        sa.Column('listing_status', sa.Enum('AVAILABLE', 'SOLD', 'RENTED', 'LEASED'), nullable=False, server_default='AVAILABLE'),
        sa.Column('transaction_type', sa.Enum('BUY', 'RESALE', 'RENT', 'LEASE'), nullable=False),
        sa.Column('property_type', sa.Enum('APARTMENT', 'VILLA', 'HOUSE', 'COMMERCIAL', 'PLOT', 'AGRICULTURAL_LAND'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('verifications',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('ruleset_version', sa.String(length=50), nullable=False),
        sa.Column('checks_json', sa.JSON(), nullable=True),
        sa.Column('result', sa.Enum('PASS', 'FAIL'), nullable=False),
        sa.Column('triggered_at', sa.DateTime(), nullable=False),
        sa.Column('triggered_by', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='SET NULL', onupdate='CASCADE'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('property_media',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('media_type', sa.String(length=50), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('uploaded_by', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='SET NULL', onupdate='SET NULL'), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('property_documents',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('document_type', sa.String(length=50), nullable=True),
        sa.Column('uploaded_by', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='SET NULL', onupdate='SET NULL'), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('enquiries',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('buyer_user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('owner_user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('status', sa.Enum('NEW', 'CONTACTED', 'CLOSED'), nullable=False, server_default='NEW'),
        sa.Column('closing_reason', sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('active_enquiries',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('enquiry_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('enquiries.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('buyer_user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('property_listings.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('started_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('buyer_user_id', 'property_listing_id', name='uk_active_buyer_listing'),
    )

    op.create_table('broker_postcode_access_current',
        sa.Column('broker_user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('postcode_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('postcodes.id', ondelete='RESTRICT', onupdate='RESTRICT'), nullable=False),
        sa.Column('granted_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('granted_by', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('broker_user_id', 'postcode_id'),
        sa.Index('idx_broker_current_broker_user', 'broker_user_id'),
        sa.Index('idx_broker_current_postcode', 'postcode_id'),
    )

    op.create_table('broker_postcode_access_history',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('broker_user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('postcode_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('postcodes.id', ondelete='RESTRICT', onupdate='RESTRICT'), nullable=False),
        sa.Column('granted_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.Column('granted_by', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_by', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='SET NULL', onupdate='SET NULL'), nullable=True),
        sa.Column('status', sa.Enum('ACTIVE', 'REVOKED', 'EXPIRED'), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('idx_broker_history_broker_user', 'broker_user_id'),
        sa.Index('idx_broker_history_postcode', 'postcode_id'),
        sa.Index('idx_broker_history_status', 'status'),
    )

    op.create_table('contact_reveal_audits',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('property_listing_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('property_listings.id', ondelete='SET NULL', onupdate='SET NULL'), nullable=True),
        sa.Column('revealed_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('activity_logs',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('entity_type', sa.String(length=50), nullable=True),
        sa.Column('entity_id', mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('audit_logs',
        sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.BIGINT(unsigned=True), sa.ForeignKey('users.id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('entity', sa.String(length=50), nullable=True),
        sa.Column('before_json', sa.JSON(), nullable=True),
        sa.Column('after_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    # Dependency-safe reverse of upgrade (drop children first, parents last)
    op.drop_table('audit_logs')
    op.drop_table('activity_logs')
    op.drop_table('contact_reveal_audits')
    op.drop_table('broker_postcode_access_history')
    op.drop_table('broker_postcode_access_current')
    op.drop_table('active_enquiries')
    op.drop_table('enquiries')
    op.drop_table('property_documents')
    op.drop_table('property_media')
    op.drop_table('verifications')
    op.drop_table('property_listings')
    op.drop_table('physical_properties')
    op.drop_table('entitlements')
    op.drop_table('subscriptions')
    op.drop_table('payment_webhook_events')
    op.drop_table('payments')
    op.drop_table('plans')
    op.drop_table('postcodes')
    op.drop_table('localities')
    op.drop_table('villages')
    op.drop_table('taluks')
    op.drop_table('districts')
    op.drop_table('cities')
    op.drop_table('admin_accounts')
    op.drop_table('role_history')
    op.drop_table('user_current_role')
    op.drop_table('auth_identities')
    op.drop_table('users')
