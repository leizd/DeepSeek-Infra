//! Typed, authenticated read-only Android platform decoding. Product state stays in Rust.
use std::time::Duration;

use deepseek_protocol::generated::deepseek::platform::v1::{
    DocumentChunk, DocumentHeader, RecognizedDocument, RenderedPdfPage, document_chunk,
    platform_ocr_engine_client::PlatformOcrEngineClient,
};
use futures_util::stream;
use sha2::{Digest, Sha256};
use tonic::Request;
use tonic::transport::Endpoint;

use crate::app_error::{AppError, codes};
use crate::extraction_control::FileProcessingControl;

const CHUNK_BYTES: usize = 65_536;
const RESPONSE_BYTES: usize = 32_000_000;
const MAX_PIXELS: u64 = 6_000_000;

pub(crate) struct PlatformOcr {
    address: String,
    credential: String,
}

impl PlatformOcr {
    pub(crate) fn from_env() -> Result<Option<Self>, AppError> {
        let address = std::env::var("DEEPSEEK_ANDROID_PLATFORM_OCR_ADDR").unwrap_or_default();
        let credential = std::env::var("DEEPSEEK_ANDROID_PLATFORM_OCR_TOKEN").unwrap_or_default();
        if address.is_empty() && credential.is_empty() && !cfg!(target_os = "android") {
            return Ok(None);
        }
        Self::new(&address, &credential).map(Some)
    }

    fn new(address: &str, credential: &str) -> Result<Self, AppError> {
        let port = address
            .strip_prefix("http://127.0.0.1:")
            .and_then(|value| value.parse::<u16>().ok())
            .filter(|value| *value != 0);
        if port.is_none()
            || !(32..=128).contains(&credential.len())
            || !credential
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        {
            return Err(unavailable(
                "Android platform OCR connection is invalid or missing.",
            ));
        }
        Ok(Self {
            address: address.to_string(),
            credential: credential.to_string(),
        })
    }

    async fn client(&self) -> Result<PlatformOcrEngineClient<tonic::transport::Channel>, AppError> {
        let channel = Endpoint::from_shared(self.address.clone())
            .map_err(|_| unavailable("Android platform OCR address is invalid."))?
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(180))
            .connect()
            .await
            .map_err(|_| unavailable("Android platform OCR connection failed."))?;
        Ok(PlatformOcrEngineClient::new(channel)
            .max_encoding_message_size(CHUNK_BYTES + 4_096)
            .max_decoding_message_size(RESPONSE_BYTES))
    }

    fn upload<'a>(
        &self,
        data: &'a [u8],
        page_index: u32,
        scale_milli: u32,
    ) -> (
        Request<impl futures_util::Stream<Item = DocumentChunk> + Send + 'static>,
        impl std::future::Future<Output = ()> + Send + 'a,
    ) {
        let header = DocumentChunk {
            part: Some(document_chunk::Part::Header(DocumentHeader {
                source_bytes: data.len() as u64,
                source_sha256: Sha256::digest(data).to_vec(),
                page_index,
                scale_milli,
            })),
        };
        let (sender, receiver) = tokio::sync::mpsc::channel::<DocumentChunk>(2);
        let body = stream::unfold(receiver, |mut receiver| async move {
            receiver.recv().await.map(|chunk| (chunk, receiver))
        });
        let producer = async move {
            if sender.send(header).await.is_err() {
                return;
            }
            for bytes in data.chunks(CHUNK_BYTES) {
                if sender
                    .send(DocumentChunk {
                        part: Some(document_chunk::Part::Data(bytes.to_vec())),
                    })
                    .await
                    .is_err()
                {
                    return;
                }
            }
        };
        let mut request = Request::new(body);
        request.metadata_mut().insert(
            "x-deepseek-platform-token",
            self.credential.parse().expect("validated ASCII credential"),
        );
        request.set_timeout(Duration::from_secs(180));
        (request, producer)
    }

    pub(crate) fn recognize(&self, data: &[u8], pdf: bool) -> Result<RecognizedDocument, AppError> {
        self.check_size(data)?;
        let document = run_platform(async {
            let mut client = self.client().await?;
            let (request, producer) = self.upload(data, 0, 0);
            let response = finish_upload(
                async {
                    if pdf {
                        client.recognize_pdf(request).await
                    } else {
                        client.recognize_image(request).await
                    }
                },
                producer,
            )
            .await;
            response
                .map(|value| value.into_inner())
                .map_err(|_| unavailable("Android platform OCR failed or timed out."))
        })?;
        validate_document(&document, data, pdf)?;
        Ok(document)
    }

    pub(crate) fn render(
        &self,
        data: &[u8],
        page: u32,
        scale: f64,
    ) -> Result<RenderedPdfPage, AppError> {
        self.check_size(data)?;
        let image = run_platform(async {
            let mut client = self.client().await?;
            let (request, producer) = self.upload(data, page, (scale * 1000.0).round() as u32);
            finish_upload(client.render_pdf_page(request), producer)
                .await
                .map(|value| value.into_inner())
                .map_err(|_| unavailable("Android PDF rendering failed or timed out."))
        })?;
        let dimensions = image.png.get(16..24).map(|bytes| {
            (
                u32::from_be_bytes(bytes[..4].try_into().expect("four-byte width")),
                u32::from_be_bytes(bytes[4..].try_into().expect("four-byte height")),
            )
        });
        if image.source_sha256 != Sha256::digest(data).as_slice()
            || image.page_index != page
            || image.total_pages < page
            || !valid_dimensions(image.width, image.height)
            || !image.png.starts_with(b"\x89PNG\r\n\x1a\n")
            || dimensions != Some((image.width, image.height))
            || !valid_png(&image.png, image.width, image.height)
        {
            return Err(unavailable(
                "Android PDF render response does not match the source.",
            ));
        }
        Ok(image)
    }

    fn check_size(&self, data: &[u8]) -> Result<(), AppError> {
        if data.is_empty() || data.len() > crate::file_upload::MAX_UPLOAD_FILE_BYTES {
            return Err(unavailable("Android platform document size is invalid."));
        }
        Ok(())
    }
}

async fn finish_upload<T>(
    rpc: impl std::future::Future<Output = T>,
    producer: impl std::future::Future<Output = ()>,
) -> T {
    tokio::pin!(rpc);
    tokio::select! {
        result = &mut rpc => result,
        _ = producer => rpc.await,
    }
}

fn run_platform<T: Send>(
    operation: impl std::future::Future<Output = Result<T, AppError>> + Send,
) -> Result<T, AppError> {
    let control = FileProcessingControl::current();
    let execute =
        move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| unavailable("Android platform processing runtime failed."))?;
            runtime.block_on(async move {
            if let Some(control) = control {
                tokio::select! {
                    biased;
                    _ = control.cancelled() => Err(crate::extraction_control::cancelled_error()),
                    result = operation => result,
                }
            } else { operation.await }
        })
        };
    if tokio::runtime::Handle::try_current().is_ok() {
        std::thread::scope(|scope| scope.spawn(execute).join())
            .map_err(|_| unavailable("Android platform processing failed."))?
    } else {
        execute()
    }
}

fn valid_png(bytes: &[u8], width: u32, height: u32) -> bool {
    if !valid_dimensions(width, height) {
        return false;
    }
    let Ok(mut reader) = png::Decoder::new(std::io::Cursor::new(bytes)).read_info() else {
        return false;
    };
    if reader.info().width != width || reader.info().height != height {
        return false;
    }
    loop {
        match reader.next_row() {
            Ok(Some(_)) => {}
            Ok(None) => return true,
            Err(_) => return false,
        }
    }
}

fn valid_dimensions(width: u32, height: u32) -> bool {
    width > 0 && height > 0 && u64::from(width) * u64::from(height) <= MAX_PIXELS
}

fn validate_document(
    document: &RecognizedDocument,
    data: &[u8],
    pdf: bool,
) -> Result<(), AppError> {
    if document.source_sha256 != Sha256::digest(data).as_slice()
        || document.total_pages == 0
        || document.pages.len() != document.total_pages as usize
        || (!pdf && document.total_pages != 1)
        || document.pages.iter().enumerate().any(|(index, page)| {
            page.page_index != index as u32 + 1
                || !valid_dimensions(page.width, page.height)
                || page
                    .candidates
                    .iter()
                    .any(|candidate| candidate.engine != "android-mlkit-chinese")
        })
    {
        return Err(unavailable(
            "Android OCR response does not match the source.",
        ));
    }
    Ok(())
}

fn unavailable(message: &str) -> AppError {
    AppError {
        message: message.to_string(),
        code: codes::OCR_UNAVAILABLE,
        status: 415,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_protocol::generated::deepseek::platform::v1::{OcrCandidate, OcrPage};

    #[tokio::test]
    async fn upload_backpressure_preserves_exact_source_and_frame_limits() {
        use futures_util::StreamExt;
        let platform = PlatformOcr::new("http://127.0.0.1:32123", &"a".repeat(32)).unwrap();
        let data = vec![42; CHUNK_BYTES * 5 + 7];
        let (request, producer) = platform.upload(&data, 0, 0);
        tokio::pin!(producer);
        // The two-slot channel cannot buffer this document without a consumer.
        assert!(
            tokio::time::timeout(Duration::from_millis(5), &mut producer)
                .await
                .is_err()
        );
        let consumer = async {
            let mut stream = Box::pin(request.into_inner());
            let header = stream.next().await.unwrap();
            let Some(document_chunk::Part::Header(header)) = header.part else {
                panic!("header first")
            };
            assert_eq!(header.source_bytes, data.len() as u64);
            assert_eq!(header.source_sha256, Sha256::digest(&data).as_slice());
            let mut bytes = Vec::new();
            while let Some(chunk) = stream.next().await {
                let Some(document_chunk::Part::Data(chunk)) = chunk.part else {
                    panic!("data only")
                };
                assert!(!chunk.is_empty() && chunk.len() <= CHUNK_BYTES);
                bytes.extend(chunk);
            }
            bytes
        };
        let ((), uploaded) = tokio::join!(producer, consumer);
        assert_eq!(uploaded, data);
    }

    #[tokio::test]
    async fn platform_processing_can_be_called_inside_an_existing_runtime() {
        assert_eq!(run_platform(async { Ok(42) }).unwrap(), 42);
        let control = FileProcessingControl::default();
        control.cancel();
        let result = control.run(|| run_platform(std::future::pending::<Result<(), AppError>>()));
        assert_eq!(result.unwrap_err().status, 499);
    }

    #[test]
    fn rendered_png_requires_a_complete_valid_image_not_just_ihdr() {
        let mut bytes = Vec::new();
        {
            let mut encoder = png::Encoder::new(&mut bytes, 2, 2);
            encoder.set_color(png::ColorType::Rgb);
            encoder.set_depth(png::BitDepth::Eight);
            encoder
                .write_header()
                .unwrap()
                .write_image_data(&[255; 12])
                .unwrap();
        }
        assert!(valid_png(&bytes, 2, 2));
        assert!(!valid_png(&bytes, 3, 2));
        assert!(!valid_png(&bytes[..24], 2, 2));
        let idat = bytes.windows(4).position(|value| value == b"IDAT").unwrap();
        bytes[idat + 5] ^= 1;
        assert!(!valid_png(&bytes, 2, 2));
    }

    #[test]
    fn platform_connection_requires_loopback_and_private_ascii_credential() {
        let token = "a".repeat(32);
        assert!(PlatformOcr::new("http://127.0.0.1:32123", &token).is_ok());
        for address in [
            "https://127.0.0.1:32123",
            "http://example.com:32123",
            "http://127.0.0.1:0",
            "http://127.0.0.1:80/path",
        ] {
            assert!(PlatformOcr::new(address, &token).is_err());
        }
        assert!(PlatformOcr::new("http://127.0.0.1:32123", "short").is_err());
        assert!(PlatformOcr::new("http://127.0.0.1:32123", &format!("{}\r\n", token)).is_err());
    }

    #[test]
    fn source_page_binding_rejects_wrong_digest_order_dimensions_and_engine() {
        let data = b"actual source";
        let page = OcrPage {
            page_index: 1,
            width: 200,
            height: 100,
            blank: false,
            candidates: vec![OcrCandidate {
                engine: "android-mlkit-chinese".into(),
                text: "Native text".into(),
            }],
        };
        let good = RecognizedDocument {
            source_sha256: Sha256::digest(data).to_vec(),
            total_pages: 1,
            pages: vec![page],
        };
        assert!(validate_document(&good, data, false).is_ok());
        assert!(validate_document(&good, b"wrong source", false).is_err());
        let mut bad = good.clone();
        bad.pages[0].page_index = 2;
        assert!(validate_document(&bad, data, true).is_err());
        let mut bad = good.clone();
        bad.pages[0].width = 6_000_001;
        assert!(validate_document(&bad, data, true).is_err());
        let mut bad = good.clone();
        bad.pages[0].candidates[0].engine = "remote".into();
        assert!(validate_document(&bad, data, true).is_err());
        let mut bad = good;
        bad.total_pages = 2;
        assert!(validate_document(&bad, data, true).is_err());
    }
}
