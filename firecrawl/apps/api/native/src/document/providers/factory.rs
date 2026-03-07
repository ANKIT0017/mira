use super::docx::DocxProvider;
use super::odt::OdtProvider;
use super::rtf::RtfProvider;
use super::DocumentProvider;
use super::xlsx::XlsxProvider;
use napi_derive::napi;

#[napi]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DocumentType {
  Docx,
  Rtf,
  Odt,
  Xlsx,
}

pub struct ProviderFactory {
  docx_provider: DocxProvider,
  rtf_provider: RtfProvider,
  odt_provider: OdtProvider,
  xlsx_provider: XlsxProvider,
}

impl ProviderFactory {
  pub fn new() -> Self {
    Self {
      docx_provider: DocxProvider::new(),
      rtf_provider: RtfProvider::new(),
      odt_provider: OdtProvider::new(),
      xlsx_provider: XlsxProvider::new(),
    }
  }

  pub fn get_provider(&self, doc_type: DocumentType) -> &dyn DocumentProvider {
    match doc_type {
      DocumentType::Docx => &self.docx_provider,
      DocumentType::Rtf => &self.rtf_provider,
      DocumentType::Odt => &self.odt_provider,
      DocumentType::Xlsx => &self.xlsx_provider,
    }
  }
}
