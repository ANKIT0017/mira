use napi::bindgen_prelude::*;
use napi_derive::napi;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[napi(object)]
pub struct PDFMetadata {
  pub num_pages: i32,
  #[serde(skip_serializing_if = "Option::is_none")]
  pub title: Option<String>,
}

fn _get_pdf_metadata(path: &str) -> std::result::Result<PDFMetadata, String> {
  let metadata = match lopdf::Document::load_metadata(path) {
    Ok(m) => m,
    Err(e) => {
      return Err(format!("Failed to load PDF metadata: {}", e));
    }
  };

  Ok(PDFMetadata {
    num_pages: metadata.page_count as i32,
    title: metadata.title,
  })
}

/// Extract metadata from PDF file.
#[napi]
pub fn get_pdf_metadata(path: String) -> Result<PDFMetadata> {
  _get_pdf_metadata(&path).map_err(|e| {
    Error::new(
      Status::GenericFailure,
      format!("Failed to get PDF metadata: {e}"),
    )
  })
}
