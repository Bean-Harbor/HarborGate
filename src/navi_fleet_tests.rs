use super::*;
use tempfile::tempdir;

fn inbound(hub: &str, token: char) -> InboundMessage {
    InboundMessage {
        platform: "whatsapp".into(),
        chat_id: "15555550101".into(),
        user_id: "15555550101".into(),
        text: format!("NAVI {hub}.{}", token.to_string().repeat(64)),
        message_id: format!("message-{hub}-{token}"),
        chat_type: "p2p".into(),
        route_key: "official-route".into(),
        session_id: "session".into(),
        mentions: vec![],
        attachments: vec![],
        metadata: Default::default(),
        timestamp: crate::models::utc_now_iso(),
        raw_payload: Value::Null,
    }
}
fn ordinary() -> InboundMessage {
    let mut input = inbound("navi-a", 'a');
    input.text = "Show my devices".into();
    input.timestamp = (chrono::Utc::now() + chrono::Duration::seconds(2)).to_rfc3339();
    input
}
fn fleet(path: &std::path::Path) -> NaviFleet {
    NaviFleet::new(
        CloudRelayClient::for_fixture("http://127.0.0.1:1"),
        path.join("routes"),
    )
}
fn proof(status: &str, id: char) -> Value {
    json!({"status":status,"session_id":id.to_string().repeat(32),"binding_id":id.to_string().repeat(32),"confirmed_at":now(),"expires_at":now()+300})
}
fn outbound(selection: Selection) -> OutboundMessage {
    OutboundMessage {
        platform: "whatsapp".into(),
        chat_id: "15555550101".into(),
        text: "Private home answer".into(),
        attachments: vec![],
        timestamp: crate::models::utc_now_iso(),
        metadata: json!({"route_key":"official-route","navi_selection":selection})
            .as_object()
            .unwrap()
            .clone(),
    }
}
fn activate(fleet: &NaviFleet, hub: &str, token: char) -> Selection {
    fleet.begin(&inbound(hub, token)).unwrap();
    let pending = fleet.claim().unwrap().unwrap();
    fleet
        .finish(
            &pending,
            StatusCode::OK,
            &proof("bound", token),
            &token.to_string().repeat(64),
        )
        .unwrap();
    fleet.select(&ordinary()).unwrap()
}

#[test]
fn ownership_revocation_persists_and_cannot_clear_a_new_generation_selection() {
    let temp = tempdir().unwrap();
    let f = fleet(temp.path());
    let old = activate(&f, "navi-0123456789ab", 'a');
    let queued = outbound(old.clone());
    assert!(f.check(&queued).is_ok());
    f.invalidate_identity(&old).unwrap();
    let restarted = fleet(temp.path());
    assert!(restarted.select(&ordinary()).is_err());
    assert!(restarted.check(&queued).is_err());
    assert!(restarted.notification_target().unwrap().is_none());
    let current = activate(&restarted, "navi-0123456789ab-g1", 'b');
    restarted.invalidate_identity(&old).unwrap();
    assert_eq!(restarted.select(&ordinary()).unwrap(), current);
    assert!(restarted.check(&queued).is_err());
}

#[test]
fn phone_proof_waits_for_owner_and_survives_restart_without_changing_active_route() {
    let temp = tempdir().unwrap();
    let f = fleet(temp.path());
    f.begin(&inbound("navi-a", 'a')).unwrap();
    let pending = f.claim().unwrap().unwrap();
    f.finish(
        &pending,
        StatusCode::OK,
        &proof("awaiting_owner_confirmation", 'a'),
        &"a".repeat(64),
    )
    .unwrap();
    assert!(f.select(&ordinary()).is_err());
    let restarted = fleet(temp.path());
    restarted
        .finish(
            &pending,
            StatusCode::OK,
            &proof("bound", 'a'),
            &"a".repeat(64),
        )
        .unwrap();
    let selected = restarted.select(&ordinary()).unwrap();
    assert_eq!(selected.hub_id, "navi-a");
    assert_eq!(selected.hub_identity, "a".repeat(64));
    assert!(restarted.check(&outbound(selected)).is_ok());
}

#[test]
fn switching_fences_queued_replies_old_phone_messages_old_confirmations_and_history() {
    let temp = tempdir().unwrap();
    let f = fleet(temp.path());
    let a = activate(&f, "navi-a", 'a');
    let queued = outbound(a.clone());
    assert!(f.check(&queued).is_ok());
    f.begin(&inbound("navi-b", 'b')).unwrap();
    let pending_b = f.claim().unwrap().unwrap();
    assert!(f.check(&queued).unwrap_err().status.is_server_error());
    f.finish(
        &pending_b,
        StatusCode::OK,
        &proof("bound", 'b'),
        &"b".repeat(64),
    )
    .unwrap();
    let b = f.select(&ordinary()).unwrap();
    assert_eq!(b.hub_id, "navi-b");
    assert_ne!(a.session_key(&ordinary()), b.session_key(&ordinary()));
    assert_eq!(
        f.check(&queued).unwrap_err().code,
        "IM_DELIVERY_NOT_ALLOWED"
    );
    f.begin(&inbound("navi-a", 'a')).unwrap(); // Previously consumed token cannot select an old home.
    assert_eq!(f.select(&ordinary()).unwrap().hub_id, "navi-b");
    let mut old = ordinary();
    old.timestamp = chrono::DateTime::from_timestamp(b.selected_at, 0)
        .unwrap()
        .to_rfc3339();
    assert!(f.select(&old).is_err());
    old = ordinary();
    old.route_key = "other-official-account".into();
    assert!(f.select(&old).is_err());
    assert!(fleet(temp.path()).check(&queued).is_err());
}

#[test]
fn late_proof_and_certificate_changes_cannot_complete_a_superseded_binding() {
    let temp = tempdir().unwrap();
    let f = fleet(temp.path());
    f.begin(&inbound("navi-a", 'a')).unwrap();
    let old = f.claim().unwrap().unwrap();
    f.begin(&inbound("navi-b", 'b')).unwrap();
    let new = f.claim().unwrap().unwrap();
    f.finish(&old, StatusCode::OK, &proof("bound", 'a'), &"a".repeat(64))
        .unwrap();
    assert!(f.select(&ordinary()).is_err());
    f.finish(
        &new,
        StatusCode::OK,
        &proof("awaiting_owner_confirmation", 'b'),
        &"b".repeat(64),
    )
    .unwrap();
    assert!(f
        .finish(&new, StatusCode::OK, &proof("bound", 'b'), &"c".repeat(64))
        .is_err());
    assert!(f
        .finish(&new, StatusCode::OK, &proof("bound", 'c'), &"b".repeat(64))
        .is_err());
    f.finish(&new, StatusCode::OK, &proof("bound", 'b'), &"b".repeat(64))
        .unwrap();
    assert_eq!(f.select(&ordinary()).unwrap().hub_id, "navi-b");
}

#[test]
fn invalid_proofs_and_expired_pending_connections_preserve_existing_selection() {
    let temp = tempdir().unwrap();
    let f = fleet(temp.path());
    let original = activate(&f, "navi-a", 'a');
    let mut bad = inbound("../../admin", 'b');
    assert!(f.begin(&bad).is_err());
    bad = inbound("navi-b", 'b');
    bad.timestamp = "2020-01-01T00:00:00Z".into();
    assert!(f.begin(&bad).is_err());
    bad = inbound("navi-b", 'b');
    bad.user_id = "15555550102".into();
    assert!(f.begin(&bad).is_err());
    f.begin(&inbound("navi-b", 'b')).unwrap();
    let pending = f.claim().unwrap().unwrap();
    f.finish(&pending, StatusCode::FORBIDDEN, &json!({}), &"b".repeat(64))
        .unwrap();
    assert!(f.check(&outbound(original.clone())).is_ok());
    f.begin(&inbound("navi-b", 'c')).unwrap();
    f.transaction(|state| {
        state
            .routes
            .values_mut()
            .next()
            .unwrap()
            .pending
            .as_mut()
            .unwrap()
            .expires_at = now() - 1;
        Ok(())
    })
    .unwrap();
    assert!(f.claim().unwrap().is_none());
    assert!(f.check(&outbound(original)).is_ok());
}

#[test]
fn route_receipts_survive_outage_and_restart_and_stale_acks_do_not_erase_a_new_switch() {
    let dir = tempdir().unwrap();
    let f = fleet(dir.path());
    activate(&f, "navi-a", 'a');
    let first = f.claim_route_update().unwrap().unwrap();
    assert_eq!(first.status, "active");
    assert!(f
        .finish_route_update(&first, StatusCode::SERVICE_UNAVAILABLE, &Value::Null)
        .is_err());
    f.begin(&inbound("navi-b", 'b')).unwrap();
    let restarted = fleet(dir.path());
    let ack = |update: &RouteUpdate| {
        json!({"binding_id":update.selection.binding_id,"generation":update.selection.generation,
        "revision":update.revision,"status":update.status,"applied":true})
    };
    restarted
        .finish_route_update(&first, StatusCode::OK, &ack(&first))
        .unwrap();
    let paused = restarted.claim_route_update().unwrap().unwrap();
    assert_eq!(paused.status, "paused");
    assert!(paused.revision > first.revision);
    let pending = restarted.claim().unwrap().unwrap();
    restarted
        .finish(
            &pending,
            StatusCode::OK,
            &proof("bound", 'b'),
            &"b".repeat(64),
        )
        .unwrap();
    restarted
        .finish_route_update(&paused, StatusCode::OK, &ack(&paused))
        .unwrap();
    let mut updates = vec![
        restarted.claim_route_update().unwrap().unwrap(),
        restarted.claim_route_update().unwrap().unwrap(),
    ];
    updates.sort_by_key(|update| update.selection.hub_id.clone());
    assert_eq!(updates[0].selection.hub_id, "navi-a");
    assert_eq!(updates[0].status, "retired");
    assert_eq!(updates[1].selection.hub_id, "navi-b");
    assert_eq!(updates[1].status, "active");
    assert_ne!(
        updates[0].selection.generation,
        updates[1].selection.generation
    );
    for update in updates {
        restarted
            .finish_route_update(&update, StatusCode::OK, &ack(&update))
            .unwrap();
    }
    assert!(restarted.claim_route_update().unwrap().is_none());
}

#[test]
fn expired_switch_queues_a_new_active_receipt_for_the_original_device() {
    let dir = tempdir().unwrap();
    let f = fleet(dir.path());
    activate(&f, "navi-a", 'a');
    f.begin(&inbound("navi-b", 'b')).unwrap();
    let paused = f.claim_route_update().unwrap().unwrap();
    assert_eq!(paused.status, "paused");
    f.transaction(|state| {
        state
            .routes
            .values_mut()
            .next()
            .unwrap()
            .pending
            .as_mut()
            .unwrap()
            .expires_at = now() - 1;
        Ok(())
    })
    .unwrap();
    assert!(f.claim().unwrap().is_none());
    let resumed = f.claim_route_update().unwrap().unwrap();
    assert_eq!(resumed.status, "active");
    assert_eq!(resumed.selection, paused.selection);
    assert!(resumed.revision > paused.revision);
}

#[test]
fn corrupt_or_redirected_directory_never_silently_reinitializes_selection() {
    let temp = tempdir().unwrap();
    let f = fleet(temp.path());
    activate(&f, "navi-a", 'a');
    let path = f.directory.join("routes.json");
    fs::write(&path, b"damaged").unwrap();
    assert!(f.select(&ordinary()).unwrap_err().status.is_server_error());
    assert_eq!(fs::read(&path).unwrap(), b"damaged");
    #[cfg(unix)]
    {
        fs::remove_file(&path).unwrap();
        let outside = temp.path().join("outside");
        fs::write(&outside, b"unchanged").unwrap();
        std::os::unix::fs::symlink(&outside, &path).unwrap();
        assert!(f.begin(&inbound("navi-b", 'b')).is_err());
        assert_eq!(fs::read(outside).unwrap(), b"unchanged");
    }
}
