use std::{error::Error, fmt};

use async_trait::async_trait;
use object_store::{
    ClientOptions,
    client::{
        HttpClient, HttpConnector, HttpError, HttpErrorKind, HttpRequest, HttpResponse, HttpService,
    },
};
use reqwest::{Client, Method, StatusCode};

#[derive(Debug, Clone)]
pub(super) struct DirectConnector(pub(super) Client);

impl HttpConnector for DirectConnector {
    fn connect(&self, _: &ClientOptions) -> object_store::Result<HttpClient> {
        Ok(HttpClient::new(self.clone()))
    }
}

#[async_trait]
impl HttpService for DirectConnector {
    async fn call(&self, request: HttpRequest) -> Result<HttpResponse, HttpError> {
        let method = request.method().clone();
        let response = HttpService::call(&self.0, request).await?;
        checked_response(&method, response)
    }
}

#[derive(Debug)]
struct WriteStatus(StatusCode);

impl fmt::Display for WriteStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "single-attempt PUT status {}", self.0.as_u16())
    }
}
impl Error for WriteStatus {}

fn checked_response(method: &Method, response: HttpResponse) -> Result<HttpResponse, HttpError> {
    let status = response.status();
    // Capture the actual response before object_store relabels 404/409 as
    // conditional failures. No global/request-shared status cache is involved.
    if *method == Method::PUT && status != StatusCode::OK {
        return Err(HttpError::new(HttpErrorKind::Decode, WriteStatus(status)));
    }
    if (*method == Method::GET || *method == Method::HEAD)
        && status.is_success()
        && (status != StatusCode::OK || response.headers().contains_key("content-range"))
    {
        return Err(HttpError::new(
            HttpErrorKind::Decode,
            std::io::Error::other("partial object response"),
        ));
    }
    if !status.is_success() {
        // SDK error parsing otherwise collects the entire untrusted body. Our
        // public errors use status only; drop the body without buffering it.
        let (parts, _) = response.into_parts();
        return Ok(HttpResponse::from_parts(parts, Vec::new().into()));
    }
    Ok(response)
}

pub(super) fn is_precondition(mut error: &(dyn Error + 'static)) -> bool {
    loop {
        if let Some(status) = error.downcast_ref::<WriteStatus>() {
            return status.0 == StatusCode::PRECONDITION_FAILED;
        }
        match error.source() {
            Some(source) => error = source,
            None => return false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use object_store::client::HttpResponse;
    use reqwest::{Method, StatusCode};

    #[test]
    fn full_object_reads_reject_partial_http_responses() {
        for status in [StatusCode::PARTIAL_CONTENT, StatusCode::NO_CONTENT] {
            let mut response = HttpResponse::new(vec![1, 2, 3].into());
            *response.status_mut() = status;
            assert!(checked_response(&Method::GET, response).is_err());
        }
        let mut response = HttpResponse::new(vec![1, 2, 3].into());
        response
            .headers_mut()
            .insert("content-range", "bytes 5-7/20".parse().unwrap());
        assert!(checked_response(&Method::GET, response).is_err());
    }

    #[tokio::test]
    async fn provider_error_bodies_are_discarded_before_sdk_collection() {
        let mut response = HttpResponse::new(vec![0; 128 * 1024].into());
        *response.status_mut() = StatusCode::BAD_GATEWAY;
        let response = checked_response(&Method::GET, response).unwrap();
        assert!(response.into_body().bytes().await.unwrap().is_empty());
    }

    #[test]
    fn only_raw_single_attempt_412_is_a_conditional_rejection() {
        for status in [
            StatusCode::NOT_FOUND,
            StatusCode::CONFLICT,
            StatusCode::PRECONDITION_FAILED,
        ] {
            let mut response = HttpResponse::new(Vec::new().into());
            *response.status_mut() = status;
            let error = checked_response(&Method::PUT, response).unwrap_err();
            assert_eq!(
                is_precondition(&error),
                status == StatusCode::PRECONDITION_FAILED
            );
        }
    }
}
