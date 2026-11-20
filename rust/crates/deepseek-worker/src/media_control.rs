//! Media authority is read from the deployment-pinned Go endpoint, not the caller.
use crate::AgentExecutionControl;
use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::media::v1::{
    BeginMediaPatchDispatchRequest, GetMediaPatchClaimRequest, MediaPatchClaim,
    MediaPatchDispatchPermit, media_mutation_control_client::MediaMutationControlClient,
};
use tonic::{Request, Status};

impl AgentExecutionControl {
    pub(crate) async fn media_claim(
        &self,
        operation: &str,
        fence: &ActionFence,
    ) -> Result<MediaPatchClaim, Status> {
        let channel = self.endpoint.connect().await.map_err(|_| unavailable())?;
        let mut client = MediaMutationControlClient::new(channel)
            .max_decoding_message_size(4 * 1024 * 1024)
            .max_encoding_message_size(4 * 1024 * 1024);
        let mut request = Request::new(GetMediaPatchClaimRequest {
            schema_version: 1,
            operation_id: operation.into(),
            fence: Some(fence.clone()),
        });
        request
            .metadata_mut()
            .insert("authorization", self.authorization.clone());
        client
            .get_claim(request)
            .await
            .map(|r| r.into_inner())
            .map_err(|_| unavailable())
    }
    pub(crate) async fn media_begin(
        &self,
        claim: &MediaPatchClaim,
        preparation: &[u8],
    ) -> Result<MediaPatchDispatchPermit, Status> {
        let channel = self.endpoint.connect().await.map_err(|_| unavailable())?;
        let mut client = MediaMutationControlClient::new(channel)
            .max_decoding_message_size(4 * 1024 * 1024)
            .max_encoding_message_size(4 * 1024 * 1024);
        let mut request = Request::new(BeginMediaPatchDispatchRequest {
            schema_version: 1,
            operation_id: claim
                .intent
                .as_ref()
                .ok_or_else(unavailable)?
                .operation_id
                .clone(),
            fence: claim.fence.clone(),
            claim_token: claim.claim_token.clone(),
            writer_fencing_token: claim.writer_fencing_token,
            preparation_sha256: preparation.to_vec(),
        });
        request
            .metadata_mut()
            .insert("authorization", self.authorization.clone());
        client
            .begin_dispatch(request)
            .await
            .map(|r| r.into_inner())
            .map_err(|_| unavailable())
    }
}
fn unavailable() -> Status {
    Status::unavailable("MEDIA_CONTROL_UNAVAILABLE")
}
