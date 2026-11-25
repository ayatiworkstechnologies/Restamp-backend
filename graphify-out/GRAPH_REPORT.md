# Graph Report - restamp-backend  (2026-11-25)

## Corpus Check
- 241 files · ~257,747 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 11 file(s) not represented in the graph (top: .csv 3, .example 2, (none) 2)

## Summary
- 4840 nodes · 13917 edges · 217 communities (149 shown, 68 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 179 edges (avg confidence: 0.9)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `82f12c02`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- schemas.py
- seed_owner_listing
- services/__init__.py
- test_agent_picker_localities.py
- routers/admin.py
- models/__init__.py
- test_schema_drift_guard.py
- post
- test_local_photo_storage.py
- services/push.py
- validate_security_config
- test_payments.py
- test_media_url_security.py
- field_validator
- agent_plans.py
- test_email.py
- worker.py
- services/subscriptions.py
- routers/conversations.py
- services/enquiries.py
- services/admin.py
- sqlalchemy_exc
- buyer.py
- services/visits.py
- localities.py
- test_property_lifecycle.py
- register_phone
- email_providers.py
- sqlalchemy
- test_photo_storage_hardening.py
- is_production
- services/payments.py
- _admin
- test_fixed_otp_production.py
- notify_safely
- test_location_master.py
- services/email.py
- services/brokers.py
- test_communication_audit.py
- push_providers.py
- test_lead_close_and_contact.py
- test_owner_isolation.py
- test_otp_core_contract.py
- users_roles.py
- Notification
- seed_listing
- test_otp_demo_mode.py
- test_phase2_fixes.py
- test_push_per_device.py
- services/location_master.py
- capabilities.py
- test_visit_names_reschedule.py
- test_push_delivery.py
- os
- test_bootstrap_admin.py
- test_owner_listings.py
- _rows
- photo_storage.py
- test_auth_production.py
- conftest.py
- buyer_plans.py
- test_location_master_workbook.py
- test_p1_correctness.py
- test_phase2_posting.py
- test_db_ssl.py
- test_phase1_capabilities.py
- test_reminders_concurrency.py
- json
- make_postcode
- pytest
- test_agent_trial.py
- test_phase3_contact_credits.py
- services/contact_credits.py
- test_google_production.py
- test_buyer_schema.py
- test_test_login.py
- test_buyer_actions_authorization.py
- test_viewed_properties.py
- re
- test_phase3_concurrency.py
- test_reminders.py
- sqlalchemy_dialects
- test_visit_legacy_lifecycle.py
- test_push.py
- test_reva.py
- test_rate_limit_m8.py
- deps.py
- services/viewed.py
- datetime
- test_migration_m2_roundtrip.py
- routers/payments.py
- MemoryProvider
- routers/conversations.py
- test_email_concurrency.py
- reminders.py
- test_owner_replies.py
- test_reminder_eligibility.py
- env.py
- routers/email.py
- agent_location_groups
- test_agent_locality_catalog.py
- routers/reva.py
- seed_dev_data.py
- test_notifications_audiences.py
- test_visit_actions.py
- test_migration_j0k1.py
- area_seeding_enabled
- email_templates.py
- test_agent_plan_localities.py
- _dealer_poster
- payment_refunds.py
- FrontendClient
- test_visit_timezone.py
- config.py
- services
- test_production_docs.py
- test_reva_helpers.py
- strip_metadata
- uuid
- serve_agaram.py
- bootstrap_admin.py
- Session
- sqlalchemy_dialects_mysql
- Locality
- RESTAMP property-photo server storage: deployment runbook
- main
- contact.py
- test_verification_policy.py
- d4e5f6a7b8c9_buyer_v1_schema.py
- test_postcode_reassignment.py
- OTP login (simplified testing OTP)
- set_my_push_preferences
- load_master
- VisitUpdate
- EnquiryListingSnapshot
- DESIGN_ONLY_GATE.md
- test_dimension_parsing_png_jpeg_webp
- test_guest_browsing.py
- admin_list
- localResources
- resolve
- ListingPhotoIn
- admin_rbac.py
- _Shared
- _enqueue
- r8s9t0u1v2w3_user_poster_type.py
- AgentPlanStatusOut
- OTPRequest
- OwnerLeadOut
- _AdminVerifyResult
- test_reva_provider_error_logs_class_only
- seed_visible_listing
- b3c4d5e6f7a8_media_lifecycle_enums.py
- AdminRejectIn
- _activity_counts_map
- j0k1l2m3n4o5_messages_notes_reschedule.py
- ActivityLog
- list_broker_listings
- admin_add_location
- p6q7r8s9t0u1_push_deliveries.py
- q7r8s9t0u1v2_subscription_management.py
- list_listings
- c9e5a1b3d7f2_rename_agent_plans_to_dealer.py
- admin_email_deliveries
- routers/users.py
- post
- backup-restore.sh
- workspace.globals.yaml
- admin_list_listings
- _buyer
- SubscriptionPlanUpdate
- check-disk.sh
- setup-storage.sh
- admin_push_deliveries
- test_reva_chat_transport_body_cap
- _Resp
- h8i9j0k1l2m3_viewed_properties.py
- t0u1v2w3x4y5_payment_refunds.py
- admin_set_contact_credit_pack
- CallEventOut
- ContactRevealOut
- dashboard
- AdminAnalyticsOut
- AdminDashboardOut
- AdminPaymentRow
- AdminPushDeviceCounts
- AdminSubscriptionRow
- AgentLocalitiesIn
- AgentTrialIn
- BrokerDashboardOut
- BrokerLocationOut
- BrokerProfileOut
- CapabilitiesOut
- ConversationCreateIn
- DraftOut
- ListingPhotoOut
- ListingStatusUpdate
- MarkConversationReadIn
- MarkMessageReadIn
- MessageCreateIn
- MyPaymentRow
- OwnerListingOut
- OwnerVisitCreate
- PaymentVerifyOut
- PhotoUploadOut
- RentTermsOut
- VerificationEventOut
- SubscriptionPlanOut
- UserSubscriptionOut
- UnreadCountOut
- VerificationRejectIn
- VisitCreate
- VisitActionsOut
- VisitOut
- ViewedPropertyOut
- unsave_listing

## God Nodes (most connected - your core abstractions)
1. `auth_header()` - 341 edges
2. `register_phone()` - 114 edges
3. `seed_owner_listing()` - 90 edges
4. `fresh_buyer()` - 88 edges
5. `_admin()` - 85 edges
6. `owner_header()` - 74 edges
7. `validate_security_config()` - 69 edges
8. `make_postcode()` - 67 edges
9. `post()` - 64 edges
10. `_rows()` - 57 edges

## Surprising Connections (you probably didn't know these)
- `OTP login (simplified testing OTP)` --references--> `otp_display_permitted()`  [INFERRED]
  README.md → app/config.py
- `OTP login (simplified testing OTP)` --references--> `fixed_otp_permitted()`  [INFERRED]
  README.md → app/config.py
- `OTP login (simplified testing OTP)` --references--> `validate_security_config()`  [INFERRED]
  README.md → app/config.py
- `test_production_with_dev_prefix_warns_not_raises()` --calls--> `validate_security_config()`  [EXTRACTED]
  tests/test_media_url_security.py → app/config.py
- `test_cors_wildcard_rejected_in_production()` --calls--> `validate_security_config()`  [EXTRACTED]
  tests/test_phase1_security.py → app/config.py

## Import Cycles
- 3-file cycle: `app/services/email.py -> app/services/reminders.py -> app/services/notifications.py -> app/services/email.py`
- 3-file cycle: `app/services/notifications.py -> app/services/push.py -> app/services/reminders.py -> app/services/notifications.py`
- 3-file cycle: `app/services/agent_plans.py -> app/services/capabilities.py -> app/services/subscriptions.py -> app/services/agent_plans.py`

## Communities (217 total, 68 thin omitted)

### Community 0 - "schemas.py"
Cohesion: 0.04
Nodes (109): AdminAgentAssignIn, AdminAgentAssignOut, AdminAgentCoverageOut, AdminAgentCoverageRow, AdminAgentLocalitiesResponse, AdminAgentLocalityOut, AdminAgentLocalityRow, AdminAgentLocalityUpdateIn (+101 more)

### Community 1 - "seed_owner_listing"
Cohesion: 0.05
Nodes (88): _set_status(), test_disabled_account_cannot_log_in_with_google(), test_disabled_account_cannot_log_in_with_otp(), test_disabled_account_is_refused_everywhere(), test_guest_browsing_unaffected(), test_reactivated_account_works_again(), test_analytics_uses_real_decisions_and_enquiries(), test_enquiries_are_read_only_and_hide_owner_notes() (+80 more)

### Community 2 - "services/__init__.py"
Cohesion: 0.08
Nodes (14): get_db(), RefreshToken, refresh_access_token(), health(), ready(), decode_token(), get_current_user(), get_session_amr() (+6 more)

### Community 3 - "test_agent_picker_localities.py"
Cohesion: 0.14
Nodes (23): MasterRow, parse_master_csv(), rows_for_names(), test_curated_csv_format_is_unchanged(), test_india_post_csv_keeps_only_chennai_tamil_nadu_rows(), test_india_post_csv_without_a_district_column_is_refused(), test_india_post_directory_format_is_parsed(), _area_seeding_on_for_these_tests() (+15 more)

### Community 4 - "routers/admin.py"
Cohesion: 0.10
Nodes (33): admin_analytics(), admin_audit_logs(), admin_change_staff_role(), admin_change_subscription_state(), admin_create_plan(), admin_dashboard(), admin_extend_subscription(), admin_get_agent_localities() (+25 more)

### Community 5 - "models/__init__.py"
Cohesion: 0.07
Nodes (40): ContactRevealAudit, AmenityMaster, ListingAmenity, ListingDraft, PhysicalProperty, PropertyDocument, PropertyListing, PropertyMedia (+32 more)

### Community 6 - "test_schema_drift_guard.py"
Cohesion: 0.06
Nodes (39): check_db(), check_db_schema(), _describe_db_error(), _project_schema_heads(), _project_schema_revisions(), _sanitize_db_error(), _assert_no_secrets(), _db_failure() (+31 more)

### Community 7 - "post"
Cohesion: 0.05
Nodes (36): add_my_lead_note(), archive_listing(), create_draft(), create_listing(), delete_draft(), delete_listing(), delete_listing_photo(), get_draft() (+28 more)

### Community 8 - "test_local_photo_storage.py"
Cohesion: 0.11
Nodes (52): _cloud_destroys(), _cloud_uploads(), _disk_files(), _insert_legacy_cloud_row(), _path_for(), test_configurable_max_bytes_enforced_in_upload(), test_configurable_max_dimension_enforced_in_upload(), test_db_failure_after_save_removes_local_file() (+44 more)

### Community 9 - "services/push.py"
Cohesion: 0.07
Nodes (31): PushDelivery, PushDevice, PushOutbox, PushPreference, admin_list(), _aggregate(), _claim(), deactivate_device() (+23 more)

### Community 10 - "validate_security_config"
Cohesion: 0.08
Nodes (46): is_valid_production_origin(), validate_security_config(), _assert_allowed(), _assert_rejected(), _fresh_flag(), test_127_loopback_allowed_only_when_explicitly_listed(), test_arbitrary_origins_rejected_with_dev_origins_configured(), test_configured_dev_origins_allowed_in_production_when_opted_in() (+38 more)

### Community 11 - "test_payments.py"
Cohesion: 0.11
Nodes (47): _needs_crisis_footer(), _captured_payload(), _checkout(), _checkout_sig(), _grants(), KEY_SECRET_ABSENT(), _locality(), _payment_count() (+39 more)

### Community 12 - "test_media_url_security.py"
Cohesion: 0.09
Nodes (40): validate_listing_image_url(), paired_locality(), _cloud_url(), _media(), strict(), test_create_photo_count_cap(), test_create_rejects_bad_url_and_stores_nothing(), test_create_rejects_duplicate_urls() (+32 more)

### Community 13 - "field_validator"
Cohesion: 0.05
Nodes (13): AdminSuspendIn, DraftCreate, DraftUpdate, LeadNoteIn, normalize_available_from(), normalize_indian_phone(), _normalize_label(), _normalize_months() (+5 more)

### Community 14 - "agent_plans.py"
Cohesion: 0.09
Nodes (30): BrokerPostcodeAccessCurrent, BrokerPostcodeAccessHistory, _activate(), activate_for_payment(), _active(), add_localities(), admin_coverage(), AgentPlan (+22 more)

### Community 15 - "test_email.py"
Cohesion: 0.10
Nodes (49): _age_codes(), _due(), _outbox(), _queued(), _run(), test_a_reclaimed_row_is_not_overwritten_by_its_old_worker(), test_admin_sees_delivery_records_without_secrets(), test_api_rejects_invalid_address_with_static_message() (+41 more)

### Community 16 - "worker.py"
Cohesion: 0.07
Nodes (33): _email_loop(), _email_worker_disabled(), multi_worker_configured(), _process_due_listings(), _push_loop(), _push_worker_disabled(), _rate_limit_cleanup_disabled(), _rate_limit_cleanup_loop() (+25 more)

### Community 17 - "services/subscriptions.py"
Cohesion: 0.12
Nodes (39): payment_bypass_enabled(), Plan, PlanVersion, Subscription, _activate(), activate_for_payment(), _active_counts(), admin_assign() (+31 more)

### Community 18 - "routers/conversations.py"
Cohesion: 0.09
Nodes (15): Conversation, ConversationReadState, Message, create_conversation(), _created_at_out(), get_conversation(), get_conversation_messages(), get_conversation_unread_count() (+7 more)

### Community 19 - "services/enquiries.py"
Cohesion: 0.10
Nodes (29): ActiveEnquiry, Enquiry, EnquiryMessage, LeadNote, add_lead_note(), _append_message(), _buyer_items(), _clean_message() (+21 more)

### Community 20 - "services/admin.py"
Cohesion: 0.13
Nodes (31): analytics(), change_staff_role(), decide_listing(), _enquiry_items(), _ensure_other_full_access_admin(), _full_access_admin_count(), get_enquiry(), get_listing() (+23 more)

### Community 21 - "sqlalchemy_exc"
Cohesion: 0.11
Nodes (20): get_broker_listing(), list_call_events(), apply_listing_filters(), apply_sort(), attach_covers(), _card_row(), escape_like_term(), fetch_cover_map() (+12 more)

### Community 22 - "buyer.py"
Cohesion: 0.09
Nodes (24): _agent_area_gate(), create_buyer_enquiry(), get_buyer_enquiry(), get_buyer_plan(), get_buyer_visit(), get_contact_credit_history(), get_contact_entitlement(), get_contact_unlock() (+16 more)

### Community 23 - "services/visits.py"
Cohesion: 0.06
Nodes (37): PropertyVisit, _ascii_digits(), buyer_entitled(), contains_contact_details(), ensure_allowed(), _has_phone(), _is_phone(), _authority() (+29 more)

### Community 24 - "localities.py"
Cohesion: 0.12
Nodes (15): _add_aliases(), _city_of(), clean_office_name(), _create_locality(), _district_of(), _get_or_create(), _india_post_offices(), list_catalog() (+7 more)

### Community 25 - "test_property_lifecycle.py"
Cohesion: 0.10
Nodes (32): admin_verify(), test_client_supplied_owner_ids_are_ignored(), test_listing_create_read_edit_remove(), test_owner_cannot_touch_another_owners_listing(), test_owner_profile_read_and_update(), test_owner_profile_update_validation_and_auth(), test_owner_visit_endpoints_reject_anonymous_and_buyers(), test_visits_are_isolated_between_owners() (+24 more)

### Community 26 - "register_phone"
Cohesion: 0.08
Nodes (41): auth_header(), register_phone(), audit_count(), seed_owner_listing(), test_auth_and_visibility_gates(), test_buyer_reveal_and_audit(), test_owner_resolution_ignores_listing_user_and_repeat_audited(), buyer_token() (+33 more)

### Community 27 - "email_providers.py"
Cohesion: 0.08
Nodes (13): DisabledProvider, EmailMessage, EmailSendError, get_provider(), LogProvider, ResendProvider, _sender(), SmtpProvider (+5 more)

### Community 28 - "sqlalchemy"
Cohesion: 0.05
Nodes (19): upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade(), upgrade() (+11 more)

### Community 29 - "test_photo_storage_hardening.py"
Cohesion: 0.06
Nodes (37): effective_media_root(), _render_disk_acknowledged(), _running_on_render(), validate_photo_storage_config(), local_mode(), test_photo_limit_startup_validation(), test_photo_storage_startup_validation(), webp_vp8x() (+29 more)

### Community 30 - "is_production"
Cohesion: 0.06
Nodes (24): is_jwt_secret_strong(), is_production(), _ensure_users_email_column(), lifespan(), dev_media_url_prefixes(), _drain(), test_env_demo_refused_even_locally(), _prod_static_media_settings() (+16 more)

### Community 31 - "services/payments.py"
Cohesion: 0.09
Nodes (18): PaymentWebhookEvent, _client(), config_public(), configured(), _confirm_and_activate(), create_checkout(), _fetch_order(), handle_webhook() (+10 more)

### Community 32 - "_admin"
Cohesion: 0.10
Nodes (42): _admin(), _audits(), _subscribe(), test_billing_reads_need_reports_view(), test_billing_reads_require_admin(), test_expired_status_is_derived_and_filters_work(), test_pagination_and_reads_never_write(), test_simulated_subscription_is_flagged_never_a_real_payment() (+34 more)

### Community 33 - "test_fixed_otp_production.py"
Cohesion: 0.11
Nodes (40): fixed_otp_in_production(), fixed_otp_permitted(), _claims(), _FakeVerifier, _fixed_otp_login(), _forge(), _grant_admin(), _local_user() (+32 more)

### Community 34 - "notify_safely"
Cohesion: 0.12
Nodes (27): _is_duplicate(), _isolated_failure(), _mysql_code(), notify_safely(), _mysql_operational(), _seed(), test_actor_and_missing_recipient_are_skipped(), test_business_flush_error_is_not_swallowed() (+19 more)

### Community 35 - "test_location_master.py"
Cohesion: 0.11
Nodes (29): read_meta(), fixture_admin_headers(), _add(), _by_pin(), _count(), _groups(), master(), test_admin_authorization() (+21 more)

### Community 36 - "services/email.py"
Cohesion: 0.18
Nodes (10): EmailOutbox, EmailVerificationCode, UserEmail, _code_hash(), _fernet(), _invalidate_codes(), _issue_code(), _pref_on() (+2 more)

### Community 37 - "services/brokers.py"
Cohesion: 0.07
Nodes (20): add_agent_plan_localities(), AgentPlanSubscribeIn, get_agent_plan(), get_broker_dashboard(), get_broker_profile(), get_broker_similar(), list_agent_locality_catalog(), list_agent_plans() (+12 more)

### Community 38 - "test_communication_audit.py"
Cohesion: 0.17
Nodes (18): _agent_for(), _future(), _notifs(), _request(), test_conversations_module_is_not_routed(), test_dealer_to_dealer_listing_is_area_gated(), test_enquiry_refused_for_ineligible_listings(), test_visit_9pm_ist_is_stored_and_returned_as_the_same_instant() (+10 more)

### Community 39 - "push_providers.py"
Cohesion: 0.07
Nodes (15): DisabledProvider, expo_error_kind(), ExpoProvider, get_provider(), LogProvider, PushMessage, PushSendError, _warn_once() (+7 more)

### Community 40 - "test_lead_close_and_contact.py"
Cohesion: 0.09
Nodes (50): _audits(), _propose(), _request(), _setup(), _status(), test_buyer_phone_is_not_reachable_by_anyone_else(), test_buyer_without_a_phone_identity_is_contact_not_available(), test_close_and_visit_cancel_are_one_transaction() (+42 more)

### Community 41 - "test_owner_isolation.py"
Cohesion: 0.23
Nodes (10): _create(), _listing_owner(), _owner(), _phone(), test_created_listing_is_owned_by_the_authenticated_principal(), test_owner_b_cannot_see_or_manage_owner_a_listing(), test_owner_drafts_are_scoped_to_their_owner(), test_owner_endpoints_require_authentication() (+2 more)

### Community 42 - "test_otp_core_contract.py"
Cohesion: 0.07
Nodes (20): create_app(), _production(), _staging(), test_captured_otp_code_helper_is_the_fixed_code(), test_correct_code_authenticates_the_requested_account(), test_development_startup_does_not_warn(), test_obsolete_otp_env_vars_are_inert(), test_production_rejects_the_fixed_otp() (+12 more)

### Community 43 - "users_roles.py"
Cohesion: 0.10
Nodes (26): AuthIdentity, AdminAccount, RoleHistory, UserCurrentRole, User, close_account(), google_auth(), google_link() (+18 more)

### Community 44 - "Notification"
Cohesion: 0.07
Nodes (25): Notification, delete_my_notification(), list_my_notifications(), mark_all_my_notifications_read(), mark_my_notification_read(), my_unread_count(), _already_notified(), _audience_set() (+17 more)

### Community 45 - "seed_listing"
Cohesion: 0.09
Nodes (24): _scoped(), seed_listing(), _seed_smart(), test_bedrooms_price_locality_filters(), test_construction_status_filter(), test_deal_and_type_filters(), test_four_deals_seed_faithful_rows_visible(), test_locality_substring_and_zero_match_for_explore() (+16 more)

### Community 46 - "test_otp_demo_mode.py"
Cohesion: 0.11
Nodes (12): _env(), is_staging(), otp_display_permitted(), otp_request(), request_code(), verify_code(), test_production_service_rejects_every_code(), test_env_demo_refused_at_real_lifespan_startup() (+4 more)

### Community 47 - "test_phase2_fixes.py"
Cohesion: 0.09
Nodes (24): fake_rzp(), FakeRazorpay, razorpay(), _Resp, _flagged_payment(), gw(), _phone(), _post() (+16 more)

### Community 48 - "test_push_per_device.py"
Cohesion: 0.20
Nodes (29): _due(), _raw_token(), _Shared, _deliveries(), _now(), _poll(), _receipt_due_now(), _run() (+21 more)

### Community 49 - "services/location_master.py"
Cohesion: 0.10
Nodes (32): _reconcile_location_master(), AgentLocationGroup, LocationMasterRecord, LocationMasterState, LocationPublishLog, add_locality(), _bad(), _commit_and_publish() (+24 more)

### Community 50 - "capabilities.py"
Cohesion: 0.13
Nodes (14): allows(), capabilities(), claim_poster_type(), _conflict(), convert_poster_type(), ensure_compatible(), get_poster_type(), _has_active_poster_subscription() (+6 more)

### Community 51 - "test_visit_names_reschedule.py"
Cohesion: 0.33
Nodes (6): _iso(), _setup(), test_names_and_listing_details_on_both_sides(), test_no_reschedule_and_status_change_keeps_rescheduled_at_clear(), test_note_edit_rules(), test_phone_default_names_are_never_returned()

### Community 52 - "test_push_delivery.py"
Cohesion: 0.17
Nodes (31): _confirmed_visit(), _device(), _outbox(), _own_row(), _queue_lead_reminder(), _queue_visit_reminder(), _queued(), _run() (+23 more)

### Community 53 - "os"
Cohesion: 0.08
Nodes (22): parse_photo_limit(), _check_storage_name(), delete_photo(), delete_stored_file(), ensure_listings_dir(), ensure_media_root(), get_media_root(), _limit() (+14 more)

### Community 54 - "test_bootstrap_admin.py"
Cohesion: 0.13
Nodes (27): _admin_role(), _bootstrap_audits(), db_conn(), _never_called(), _new_user(), no_admins(), _run(), _input() (+19 more)

### Community 55 - "test_owner_listings.py"
Cohesion: 0.10
Nodes (37): fresh_pincode(), _create_listing(), _db_row(), _db_status(), _post_listing(), _setup_owner(), _setup_owner_with_phone(), test_amenity_master_seed_and_link() (+29 more)

### Community 56 - "_rows"
Cohesion: 0.15
Nodes (27): test_unverified_or_absent_address_gets_no_email_but_keeps_in_app(), test_body_and_title_respect_column_limits(), test_db_error_inside_notification_does_not_break_enquiry(), test_exception_inside_notification_does_not_break_visit_or_lead_close(), test_failed_business_request_leaves_no_notification(), _assert_clean(), _enquiry_id(), _pair() (+19 more)

### Community 57 - "photo_storage.py"
Cohesion: 0.20
Nodes (6): delete_uploaded_image(), _ensure_configured(), upload_listing_image(), max_image_bytes(), size_exceeded_detail(), validate_image_upload()

### Community 58 - "test_auth_production.py"
Cohesion: 0.10
Nodes (16): DevGoogleVerifier, DisabledGoogleVerifier, _get_jwks_client(), get_verifier(), GoogleVerifier, ProductionGoogleVerifier, _prod(), test_prod_client_id_selects_production_verifier() (+8 more)

### Community 59 - "conftest.py"
Cohesion: 0.12
Nodes (19): _blocked_db_names(), check_migration_head(), check_test_database_allowed(), client(), _override(), _guard_test_database(), _parse_db_name(), _StubConn (+11 more)

### Community 60 - "buyer_plans.py"
Cohesion: 0.15
Nodes (15): Payment, activate_for_payment(), _active_paid(), BuyerPlan, catalogue(), ensure_can_reveal(), _expire_previous(), _from_row() (+7 more)

### Community 61 - "test_location_master_workbook.py"
Cohesion: 0.08
Nodes (25): build_workbook(), content_digest(), display_name(), _group_rows(), group_source(), normalize_name(), read_primary_names(), read_source_workbook() (+17 more)

### Community 62 - "test_p1_correctness.py"
Cohesion: 0.11
Nodes (19): _allocate_pincode(), _b_build_chain(), _b_close_after(), _b_delete_chain(), _b_delete_user_row(), _create_listing(), _owner_token(), _p1_session_end_cleanup() (+11 more)

### Community 63 - "test_phase2_posting.py"
Cohesion: 0.27
Nodes (25): _assign(), _caps(), _expire(), _owner_checkout(), _phone(), _plan(), _plan_required(), _post() (+17 more)

### Community 64 - "test_db_ssl.py"
Cohesion: 0.14
Nodes (24): build_engine_kwargs(), create_app_engine(), _is_local_url(), _norm_key(), normalize_database_url(), _resolve_ssl_ca(), _dummy_ca(), test_bare_mysql_scheme_normalised_to_pymysql() (+16 more)

### Community 65 - "test_phase1_capabilities.py"
Cohesion: 0.18
Nodes (27): _assign(), _caps(), _phone(), _plan(), _poster_type(), _register(), _sub_count(), test_account_type_never_changes_an_existing_account() (+19 more)

### Community 66 - "test_reminders_concurrency.py"
Cohesion: 0.23
Nodes (16): process_outbox(), run_scan(), _claim_and_die(), _count(), _deliver(), _expire_lease(), _in_threads(), _q() (+8 more)

### Community 67 - "json"
Cohesion: 0.15
Nodes (22): _agent_for(), _buyer(), _listing(), _owner_in_buyer_mode(), test_agent_calls_keep_the_agent_role(), test_agent_visit_reaches_the_owner_as_agent(), test_buyer_quota_and_agent_unlimited_reveals_are_unchanged(), test_buyer_visit_is_saved_listed_and_reaches_the_owner_as_buyer() (+14 more)

### Community 68 - "make_postcode"
Cohesion: 0.20
Nodes (21): make_postcode(), test_dashboard_counts_are_real(), _grant(), _granter(), _listing(), _phone(), test_agents_are_isolated_from_each_other(), test_broker_routes_are_read_only() (+13 more)

### Community 69 - "pytest"
Cohesion: 0.13
Nodes (18): captured_otp_code(), ApiError, map_error(), test_legacy_env_flags_are_inert_and_contract_preserved(), _login_as(), test_auth_lookup_paths_are_indexed_and_fast(), test_e2e_admin(), test_e2e_google_rules() (+10 more)

### Community 70 - "test_agent_trial.py"
Cohesion: 0.17
Nodes (20): _add_postcode_to_locality(), _area(), _area_refused(), _ids(), _owner_with_listings(), _phone(), _setup(), test_agent_actions_outside_the_area_are_refused_and_write_nothing() (+12 more)

### Community 71 - "test_phase3_contact_credits.py"
Cohesion: 0.22
Nodes (25): _buy_pack(), _buyer_plan(), _counts(), _dealer_covering(), _listings(), _login(), _phone(), _price_pack() (+17 more)

### Community 72 - "services/contact_credits.py"
Cohesion: 0.16
Nodes (16): ContactCreditAccount, ContactCreditLedger, PropertyContactUnlock, get_unlock(), grant_for_payment(), _legacy_plan_room(), list_ledger(), list_unlocks() (+8 more)

### Community 73 - "test_google_production.py"
Cohesion: 0.16
Nodes (18): create_access_token(), _phone_user(), _rsa_keypair(), _rsa_token(), _stub_jwks(), test_decode_wiring_audience_issuer_algorithm(), test_empty_token_is_401(), test_link_cross_user_409() (+10 more)

### Community 74 - "test_buyer_schema.py"
Cohesion: 0.09
Nodes (8): _FakeBind, _FakeScalar, _migration_module(), test_f1_guard_allows_empty_table(), test_f1_guard_blocks_populated_table_clearly(), test_f1_guard_runs_before_any_schema_op(), test_migration_chain_and_ops(), test_viewed_properties_shape()

### Community 75 - "test_test_login.py"
Cohesion: 0.10
Nodes (13): _identity_count(), _phone_suffix_body(), _prod(), test_legacy_keyed_login_shape_is_plain_404(), test_no_module_no_settings(), test_normal_otp_flow_still_authenticates(), test_otp_request_schema_rejects_stray_code_field(), test_otp_request_schema_rejects_stray_code_field_tight() (+5 more)

### Community 76 - "test_buyer_actions_authorization.py"
Cohesion: 0.20
Nodes (18): grant_test_posting_plan(), _buyer(), _current_role(), _enquiry_body(), _listing(), _owner(), _register(), test_buyer_creates_enquiry() (+10 more)

### Community 77 - "test_viewed_properties.py"
Cohesion: 0.20
Nodes (23): _buyer_view_header(), _kept_listing_ids(), _seed_listing(), _seed_tied_views(), test_isolation_between_buyers(), test_listing_that_loses_visibility_is_omitted_not_leaked(), test_migrated_table_enforces_uniqueness_and_cascades(), test_record_view_first_time() (+15 more)

### Community 78 - "re"
Cohesion: 0.15
Nodes (10): SavedProperty, _count(), _first_deal(), _first_type(), _max_price_paise(), _parse_locality(), parse_search_filters(), public_listing_snapshot() (+2 more)

### Community 79 - "test_phase3_concurrency.py"
Cohesion: 0.21
Nodes (12): _alembic(), _parallel(), _set_account(), _state(), test_last_credit_cannot_be_spent_twice(), test_last_free_unlock_cannot_be_spent_twice(), test_migration_backfills_existing_reveals(), test_parallel_payment_confirmations_credit_once() (+4 more)

### Community 80 - "test_reminders.py"
Cohesion: 0.20
Nodes (21): _age_enquiry(), _confirmed_visit(), _new_enquiry(), _scan(), test_1h_window_queues_only_the_1h_reminder(), test_24h_window_queues_for_buyer_and_owner(), test_lead_reminder_fires_at_the_2h_offset_only(), test_lead_reminder_push_payload_carries_the_offset() (+13 more)

### Community 81 - "sqlalchemy_dialects"
Cohesion: 0.14
Nodes (3): upgrade(), upgrade(), Entitlement

### Community 82 - "test_visit_legacy_lifecycle.py"
Cohesion: 0.32
Nodes (19): _patch(), _assert_persisted(), _legacy(), _row(), test_closed_lead_cancels_open_legacy_visits_and_freezes_them(), test_confirmed_cancel_by_either_party(), test_confirmed_completed_by_owner_only_and_silent(), test_dealer_poster_follows_owner_side_rules() (+11 more)

### Community 83 - "test_push.py"
Cohesion: 0.17
Nodes (21): _fernet(), _secret(), _register(), test_account_push_is_mandatory(), test_admin_sees_push_deliveries_without_secrets(), test_available_reports_the_configured_provider(), test_deactivate_is_idempotent_and_scoped_to_the_caller(), test_invalid_tokens_and_platforms_are_rejected_with_422() (+13 more)

### Community 84 - "test_reva.py"
Cohesion: 0.11
Nodes (9): test_reva_chat_agaram_endpoint_integration(), test_reva_chat_demo_plans_quote_only_real_catalogue(), test_reva_chat_invalid_token_fails_closed(), test_reva_chat_rate_limit_shared_storage(), test_reva_transcribe_demo(), test_reva_transcribe_demo_tamil(), test_reva_transcribe_rate_limit(), test_reva_transcribe_rejects_bad_extension_and_type() (+1 more)

### Community 85 - "test_rate_limit_m8.py"
Cohesion: 0.05
Nodes (52): _resolve_proxy_trust(), RateLimitWindow, check_and_hit(), cleanup_expired_windows(), client_ip(), comm_limit(), _hmac_key(), ip_bucket_id() (+44 more)

### Community 86 - "deps.py"
Cohesion: 0.11
Nodes (16): _refuse_fixed_otp_session(), require_admin(), require_admin_permission(), _check(), require_buyer(), require_owner_of_physical_property(), require_role(), _check() (+8 more)

### Community 87 - "services/viewed.py"
Cohesion: 0.16
Nodes (9): ViewedProperty, _enforce_retention(), _items(), list_viewed(), _newest_first(), record_view(), _utcnow_naive_seconds(), _viewed_at_out() (+1 more)

### Community 88 - "datetime"
Cohesion: 0.18
Nodes (12): _counts(), _future(), test_buyer_visit_note_with_phone_is_allowed(), test_patch_visit_note_edits_remain_refused(), test_reply_allowed_after_the_buyer_unlocks_this_exact_property(), test_reply_with_contact_details_before_unlock_is_refused_without_side_effects(), test_unlocking_a_different_property_does_not_authorize_disclosure(), test_visit_proposal_note_allowed_after_unlock_and_plain_notes_always() (+4 more)

### Community 89 - "test_migration_m2_roundtrip.py"
Cohesion: 0.15
Nodes (12): _alembic(), _cleanup(), _column_type(), _db_name(), _engine(), _free_pincode(), _guard(), _schema() (+4 more)

### Community 90 - "routers/payments.py"
Cohesion: 0.14
Nodes (9): _active_user(), CheckoutIn, create_checkout(), get_config(), payment_history(), payment_invoice(), payment_webhook(), verify_payment() (+1 more)

### Community 91 - "MemoryProvider"
Cohesion: 0.07
Nodes (17): MemoryProvider, set_provider_for_tests(), mask_token(), MemoryProvider, set_provider_for_tests(), mail(), mailprov(), pushprov() (+9 more)

### Community 92 - "routers/conversations.py"
Cohesion: 0.09
Nodes (13): api_create_conversation(), api_get_conversation(), api_get_conversation_messages(), api_get_unread_count(), api_list_conversations(), api_mark_conversation_read(), api_mark_message_read(), api_send_message() (+5 more)

### Community 93 - "test_email_concurrency.py"
Cohesion: 0.14
Nodes (8): queued(), _SlowProvider, test_two_workers_send_every_email_exactly_once(), worker(), _race(), _stored(), test_concurrent_claims_exactly_one_wins(), test_concurrent_try_claims_exactly_one_true()

### Community 94 - "reminders.py"
Cohesion: 0.21
Nodes (10): _active(), _iso(), lead_reminder_drop_reason(), _owner_replied(), reminder_drop_reason(), run_lead_scan(), run_visit_scan(), _utcnow_naive() (+2 more)

### Community 95 - "test_owner_replies.py"
Cohesion: 0.31
Nodes (17): _enquire(), _lead(), _reply(), _rows(), test_buyer_sees_owner_replies_in_order(), test_closed_lead_refuses_replies(), test_listing_ownership_mismatch_fails_closed(), test_message_validation() (+9 more)

### Community 96 - "test_reminder_eligibility.py"
Cohesion: 0.23
Nodes (19): process_outbox(), _assert_dropped(), _both_channels(), _deliver_all(), _lead_setup(), _rows(), _scan(), test_buyer_account_closed_between_scan_and_delivery() (+11 more)

### Community 97 - "env.py"
Cohesion: 0.15
Nodes (10): _compile_bigint(), _compile_decimal(), _compile_enum(), _compile_integer(), _compile_mediumint(), _compile_smallint(), _compile_tinyint(), load_dotenv() (+2 more)

### Community 98 - "routers/email.py"
Cohesion: 0.17
Nodes (8): is_development(), my_email(), _out(), remove_my_email(), resend_my_verification(), set_my_email(), set_my_email_preferences(), verify_my_email()

### Community 100 - "test_agent_locality_catalog.py"
Cohesion: 0.19
Nodes (19): _admin(), _area_seeding_on_for_these_tests(), _audits(), _count(), _ip_csv(), _starter(), test_add_within_allowance_without_payment_and_sixth_refused(), test_admin_assignment_requires_a_plan_and_a_real_agent() (+11 more)

### Community 101 - "routers/reva.py"
Cohesion: 0.08
Nodes (20): chat_endpoint(), chat_health(), _chat_rate_limits(), ChatHistoryItem, ChatRequest, ChatResponse, _demo_reply(), _extract_reply() (+12 more)

### Community 102 - "seed_dev_data.py"
Cohesion: 0.09
Nodes (16): clean(), clean_locations(), clean_rows(), _engine(), _ensure_home_batch(), _get_or_create_locality(), _get_or_create_postcode(), _get_seed_image() (+8 more)

### Community 103 - "test_notifications_audiences.py"
Cohesion: 0.20
Nodes (12): _expected(), _get(), _seed(), test_duplicate_audience_values_do_not_duplicate_rows_or_counts(), test_every_repeated_value_is_honoured_not_only_the_last(), test_list_and_unread_count_use_identical_semantics(), test_pagination_over_a_multi_audience_union(), test_read_all_affects_only_the_selected_audiences() (+4 more)

### Community 104 - "test_visit_actions.py"
Cohesion: 0.18
Nodes (12): _detail(), _enquiry_queries(), _list_item(), _sides(), test_advertised_actions_execute_and_others_are_refused(), test_closed_lead_exposes_no_actions_and_refuses_changes(), test_create_responses_carry_creator_actions(), test_existing_fields_unchanged_plus_actions() (+4 more)

### Community 105 - "test_migration_j0k1.py"
Cohesion: 0.17
Nodes (12): _expected_heads(), test_migration_graph_single_head_and_parent(), _columns(), _fks(), _indexes(), _script(), test_graph_single_head_and_parent(), test_new_tables_match_model() (+4 more)

### Community 106 - "area_seeding_enabled"
Cohesion: 0.19
Nodes (8): area_seeding_enabled(), _is_local_database_url(), _area_seeding_on(), pending_review(), main(), main(), test_flag_is_off_by_default_and_never_on_in_production(), test_local_db_url_detection()

### Community 107 - "email_templates.py"
Cohesion: 0.18
Nodes (10): _base_url(), _html(), link_for(), render(), _support_line(), _Template, _text(), test_links_are_fixed_paths_on_the_configured_https_site_only() (+2 more)

### Community 108 - "test_agent_plan_localities.py"
Cohesion: 0.28
Nodes (14): _agent(), _count(), _places(), _starter_with_three(), test_add_fourth_and_fifth_without_payment(), test_agents_are_isolated(), test_auth_and_validation(), test_batch_over_the_limit_is_refused_whole() (+6 more)

### Community 109 - "_dealer_poster"
Cohesion: 0.18
Nodes (13): grant_posting_plan_for_user(), _dealer_poster(), _dealer_with_visit(), _role(), test_another_dealer_cannot_see_a_dealers_leads(), test_another_dealer_cannot_see_or_change_a_dealers_visits(), test_buyer_cannot_use_owner_visit_endpoints(), test_dealer_cannot_reschedule() (+5 more)

### Community 110 - "payment_refunds.py"
Cohesion: 0.24
Nodes (10): PaymentRefund, AuditLog, _apply_gateway_refund(), apply_refund_webhook(), _intended_type(), _jsonable(), list_reviews(), _now() (+2 more)

### Community 111 - "FrontendClient"
Cohesion: 0.19
Nodes (3): FrontendClient, fx(), test_api_base_url_env_config()

### Community 112 - "test_visit_timezone.py"
Cohesion: 0.28
Nodes (10): _as_utc_instant(), _buyer_visit(), _stored(), test_get_returns_the_same_instant_as_create(), test_naive_input_is_treated_as_utc(), test_near_future_offset_instant_is_accepted(), test_offset_input_is_stored_and_returned_as_the_same_utc_instant(), test_past_instant_is_rejected_whatever_its_offset() (+2 more)

### Community 113 - "config.py"
Cohesion: 0.11
Nodes (9): DBConfig, is_valid_localhost_origin(), is_valid_media_public_base_url(), _load_local_dotenv(), parse_cors_origins(), production_local_dev_origins(), Settings, _cors_settings() (+1 more)

### Community 114 - "services"
Cohesion: 0.13
Nodes (14): generateValue, key, sync, value, services, buildCommand, envVars, healthCheckPath (+6 more)

### Community 115 - "test_production_docs.py"
Cohesion: 0.26
Nodes (10): _app(), _production(), test_docs_disabled_in_production_by_default(), test_docs_enabled_in_production_with_opt_in(), test_local_development_docs_unchanged(), test_otp_still_never_returns_a_code_in_production(), test_protected_endpoints_still_require_authentication(), test_role_rules_unchanged_with_docs_enabled() (+2 more)

### Community 116 - "test_reva_helpers.py"
Cohesion: 0.16
Nodes (8): decode_token_user_id(), register_buyer(), suspend_user(), switch_role(), test_reva_chat_provider_prompt_forbids_invention(), test_reva_chat_rate_limit_per_user_bucket(), test_reva_chat_signed_in_role_comes_from_database(), test_reva_chat_suspended_account_fails_closed()

### Community 117 - "strip_metadata"
Cohesion: 0.13
Nodes (14): _exif_orientation(), _keep_jpeg_segment(), _orientation_tiff(), _strip_jpeg(), strip_metadata(), _strip_png(), _strip_webp(), image_dimensions() (+6 more)

### Community 118 - "uuid"
Cohesion: 0.37
Nodes (9): _count(), _free_pins(), _ins(), _plan(), _seed_chain(), test_any_outside_reference_refuses_the_whole_clean(), test_clean_deletes_only_the_seed_chain_never_same_named_real_rows(), test_seed_owners_own_property_does_not_block() (+1 more)

### Community 119 - "serve_agaram.py"
Cohesion: 0.20
Nodes (6): chat_completions(), ChatChoice, ChatCompletionRequest, ChatCompletionResponse, ChatMessage, health()

### Community 120 - "bootstrap_admin.py"
Cohesion: 0.26
Nodes (6): bootstrap_admin(), check_database_url(), confirmation_phrase(), main(), _parse_args(), Refused

### Community 121 - "Session"
Cohesion: 0.17
Nodes (12): _claim(), _deliver(), _finish(), get_status(), normalize_email(), _prepare(), remove_email(), resend_code() (+4 more)

### Community 122 - "sqlalchemy_dialects_mysql"
Cohesion: 0.23
Nodes (5): upgrade(), downgrade(), upgrade(), downgrade(), upgrade()

### Community 123 - "Locality"
Cohesion: 0.30
Nodes (12): City, District, Locality, LocalityAlias, Postcode, Taluk, Village, _existing_locality_for() (+4 more)

### Community 124 - "RESTAMP property-photo server storage: deployment runbook"
Cohesion: 0.17
Nodes (11): 10. Render, 1. How it works, 2. Inspect before changing anything, 3. Storage directory, 4. Backend service, 5. Nginx, 6. Backups: required before calling storage production-ready, 7. Disk monitoring (+3 more)

### Community 125 - "main"
Cohesion: 0.22
Nodes (6): unsafe_media_root_reason(), _within(), status_summary(), storage_problem(), main(), test_storage_path_rules()

### Community 126 - "contact.py"
Cohesion: 0.38
Nodes (3): find_owner(), find_owner_phone(), reveal_contact()

### Community 127 - "test_verification_policy.py"
Cohesion: 0.32
Nodes (8): test_a_row_locked_by_another_worker_is_skipped_not_waited_for(), _create(), _owner(), _state(), test_new_listing_is_pending_and_stays_pending_without_an_explicit_action(), test_only_the_explicit_verify_transition_advances_the_listing(), test_publishing_requires_a_verified_listing(), test_rejected_listing_returns_to_pending_through_explicit_resubmit()

### Community 128 - "d4e5f6a7b8c9_buyer_v1_schema.py"
Cohesion: 0.27
Nodes (4): _assert_empty_property_listings(), _guard_bind(), NotEmptyForNotNull, upgrade()

### Community 129 - "test_postcode_reassignment.py"
Cohesion: 0.27
Nodes (13): _area_seeding_on_for_these_tests(), _free_pins(), _ins(), _owner_of(), _placeholder(), _reassign(), _setup(), test_any_reference_refuses_and_changes_nothing() (+5 more)

### Community 130 - "OTP login (simplified testing OTP)"
Cohesion: 0.22
Nodes (8): Local setup, OTP login (simplified testing OTP), Production configuration, Restamp-backend, Run the backend locally, Running the test suite (local `restamp_test`), Staging (fixed testing OTP for testers), Test OTP login locally (curl)

### Community 131 - "set_my_push_preferences"
Cohesion: 0.20
Nodes (4): deactivate_my_device(), my_push(), register_my_device(), set_my_push_preferences()

### Community 132 - "load_master"
Cohesion: 0.18
Nodes (10): is_chennai_service_pincode(), load_master(), test_service_policy_is_the_range_plus_only_approved_pincodes(), _script(), test_admin_locality_endpoints_are_hidden(), test_aliases_are_ignored_while_paused(), test_operator_scripts_refuse_and_change_nothing(), test_owner_pincode_fix_and_agent_add_locations_stay_active() (+2 more)

### Community 134 - "EnquiryListingSnapshot"
Cohesion: 0.29
Nodes (4): BuyerEnquiryListingSnapshot, EnquiryListingSnapshot, OwnerLeadListingSnapshot, VisitListingSnapshot

### Community 135 - "DESIGN_ONLY_GATE.md"
Cohesion: 0.29
Nodes (6): and any backend beyond approved DB models is NOT authorized., Implementation of SQLAlchemy models, Alembic, MySQL, APIs, auth,, payments, business services, AI/chat/site-visit/negotiation/feed,, RESTAMP Phase 1 Implementation — Design-Only Gate Note, See RESTAMP_DATABASE_ARCHITECTURE_V2.4.2_FINAL.md for the authoritative design., This file is a placeholder confirming the design-only constraint.

### Community 136 - "test_dimension_parsing_png_jpeg_webp"
Cohesion: 0.29
Nodes (4): png_bytes(), test_dimension_parsing_png_jpeg_webp(), webp_vp8(), webp_vp8l()

### Community 137 - "test_guest_browsing.py"
Cohesion: 0.27
Nodes (13): seed_verified(), _assert_no_phone_leak(), _create(), _owner(), _seed_all_deals(), test_authenticated_flows_preserved_without_leaks(), test_enquiry_403_means_wrong_role(), test_guest_browses_all_four_deal_types() (+5 more)

### Community 138 - "admin_list"
Cohesion: 0.33
Nodes (3): admin_list(), mask_email(), test_mask_email()

### Community 139 - "localResources"
Cohesion: 0.33
Nodes (5): localResources, collections, environments, workspace, id

### Community 140 - "resolve"
Cohesion: 0.26
Nodes (9): name_key(), resolve(), Resolved, _free_pins(), _key_rows(), test_existing_adyar_anna_nagar_t_nagar_are_reused_not_duplicated(), test_kodambakkam_and_perambur_resolve_to_canonical_records(), test_master_load_never_rebinds_or_invents() (+1 more)

### Community 142 - "admin_rbac.py"
Cohesion: 0.29
Nodes (4): admin_profile(), has_permission(), permissions_for(), get_admin_role()

### Community 144 - "_enqueue"
Cohesion: 0.22
Nodes (6): _deliverable_address(), delivery_available(), _enqueue(), enqueue_account_suspended(), enqueue_for_notification(), _mysql_code()

### Community 147 - "OTPRequest"
Cohesion: 0.50
Nodes (3): OTPRequest, OTPVerify, test_otp_schemas_accept_indian_mobile_only()

### Community 154 - "AdminRejectIn"
Cohesion: 0.20
Nodes (4): _admin_text(), AdminReactivateIn, AdminRejectIn, AdminVerifyIn

### Community 155 - "_activity_counts_map"
Cohesion: 0.33
Nodes (3): agent_call_counts(), call_counts(), _activity_counts_map()

### Community 157 - "ActivityLog"
Cohesion: 0.40
Nodes (4): ActivityLog, record_call_event(), _out(), record_call_tap()

### Community 163 - "c9e5a1b3d7f2_rename_agent_plans_to_dealer.py"
Cohesion: 0.83
Nodes (3): downgrade(), _rename(), upgrade()

### Community 165 - "routers/users.py"
Cohesion: 0.20
Nodes (13): admin_grant(), admin_ping(), broker_scope(), buyer_only(), get_user(), my_capabilities(), my_history(), my_role() (+5 more)

### Community 166 - "post"
Cohesion: 0.10
Nodes (11): admin_assign_agent_localities(), admin_assign_subscription(), admin_convert_poster_type(), admin_grant_staff(), admin_reactivate_user(), admin_refund_payment(), admin_reject_listing(), admin_suspend_user() (+3 more)

### Community 170 - "_buyer"
Cohesion: 0.18
Nodes (19): _buyer(), _enquire(), _owner(), _reply(), _stored_senders(), test_cross_account_isolation_and_spoofed_identity_fields_are_ignored(), test_dealer_buyer_side_to_owner_and_owner_reply(), test_dealer_in_owner_mode_and_buyer_two_way_with_isolation() (+11 more)

## Knowledge Gaps
- **68 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `auth_header()` connect `register_phone` to `seed_owner_listing`, `test_agent_picker_localities.py`, `load_master`, `test_local_photo_storage.py`, `test_guest_browsing.py`, `test_payments.py`, `test_media_url_security.py`, `test_email.py`, `test_property_lifecycle.py`, `is_production`, `_admin`, `test_fixed_otp_production.py`, `notify_safely`, `test_location_master.py`, `test_communication_audit.py`, `test_lead_close_and_contact.py`, `test_owner_isolation.py`, `_buyer`, `test_otp_core_contract.py`, `seed_listing`, `test_phase2_fixes.py`, `test_visit_names_reschedule.py`, `test_owner_listings.py`, `_rows`, `conftest.py`, `test_p1_correctness.py`, `test_phase2_posting.py`, `test_phase1_capabilities.py`, `json`, `make_postcode`, `pytest`, `test_agent_trial.py`, `test_phase3_contact_credits.py`, `test_google_production.py`, `test_buyer_actions_authorization.py`, `test_viewed_properties.py`, `test_push.py`, `test_rate_limit_m8.py`, `test_owner_replies.py`, `test_agent_locality_catalog.py`, `test_notifications_audiences.py`, `test_agent_plan_localities.py`, `_dealer_poster`, `test_visit_timezone.py`, `test_production_docs.py`, `test_reva_helpers.py`, `test_verification_policy.py`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Should `schemas.py` be split into smaller, more focused modules?**
  _Cohesion score 0.03523166023166023 - nodes in this community are weakly interconnected._
- **Why does `PropertyVisit` connect `services/visits.py` to `test_reminders_concurrency.py`, `models/__init__.py`, `test_communication_audit.py`, `test_lead_close_and_contact.py`, `re`, `test_reminders.py`, `sqlalchemy_dialects`, `services/admin.py`, `test_push_delivery.py`, `MemoryProvider`, `reminders.py`?**
  _High betweenness centrality (0.016) - this node is a cross-community bridge._
- **Should `seed_owner_listing` be split into smaller, more focused modules?**
  _Cohesion score 0.05053515783769137 - nodes in this community are weakly interconnected._
- **Why does `register_phone()` connect `register_phone` to `seed_owner_listing`, `test_agent_picker_localities.py`, `load_master`, `test_guest_browsing.py`, `test_payments.py`, `test_media_url_security.py`, `test_property_lifecycle.py`, `is_production`, `_admin`, `test_fixed_otp_production.py`, `test_location_master.py`, `test_communication_audit.py`, `test_lead_close_and_contact.py`, `test_owner_isolation.py`, `_buyer`, `seed_listing`, `test_owner_listings.py`, `conftest.py`, `test_p1_correctness.py`, `test_phase1_capabilities.py`, `json`, `make_postcode`, `pytest`, `test_agent_trial.py`, `test_buyer_actions_authorization.py`, `test_viewed_properties.py`, `test_agent_locality_catalog.py`, `test_agent_plan_localities.py`, `_dealer_poster`, `test_production_docs.py`, `test_reva_helpers.py`, `test_verification_policy.py`?**
  _High betweenness centrality (0.012) - this node is a cross-community bridge._
- **Should `services/__init__.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08065458796025717 - nodes in this community are weakly interconnected._
- **Should `test_agent_picker_localities.py` be split into smaller, more focused modules?**
  _Cohesion score 0.14482758620689656 - nodes in this community are weakly interconnected._