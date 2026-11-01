use serde_json::{json, Value};

/// Keep the server's authentication/eligibility diagnosis separate from tokens.
/// Only the account verification URL is retained; never persist raw responses.
#[derive(Debug)]
pub struct CloudCodeError {
    pub status: Option<u16>,
    pub reason: String,
    message: String,
    validation_url: String,
}

pub fn google_validation_url(value: &str) -> Option<String> {
    let url = reqwest::Url::parse(value).ok()?;
    (url.scheme() == "https"
        && url.host_str() == Some("accounts.google.com")
        && url.username().is_empty()
        && url.password().is_none()
        && url.port_or_known_default() == Some(443))
        .then(|| url.to_string())
}

impl CloudCodeError {
    pub fn from_http(method: &str, status: u16, body: &str) -> Self {
        let parsed: Value = serde_json::from_str(body).unwrap_or(Value::Null);
        let error = &parsed["error"];
        let mut reason = error["status"].as_str().unwrap_or_default().to_string();
        let message = error["message"].as_str().unwrap_or("服务端未提供错误详情");
        let mut validation_url = String::new();
        for detail in error["details"].as_array().into_iter().flatten() {
            if detail["@type"].as_str() != Some("type.googleapis.com/google.rpc.ErrorInfo") {
                continue;
            }
            if let Some(value) = detail["reason"].as_str().filter(|s| !s.is_empty()) {
                reason = value.to_string();
            }
            if let Some(url) = detail["metadata"]["validation_url"].as_str()
                .and_then(google_validation_url)
            {
                validation_url = url;
            }
        }
        // A verification link is meaningful only when the server requests it.
        if status != 403 || reason != "VALIDATION_REQUIRED" {
            validation_url.clear();
        }
        Self {
            status: Some(status), reason,
            message: format!("{method}: {}", message.chars().take(300).collect::<String>()),
            validation_url,
        }
    }

    pub fn quota_failure(&self, tier: &Value, fetched_at: &str) -> Value {
        let code = if self.reason.is_empty() {
            match self.status {
                Some(401) => "UNAUTHENTICATED",
                Some(403) => "PERMISSION_DENIED",
                _ => "",
            }
        } else {
            &self.reason
        };
        json!({
            "fetched_at": fetched_at, "tier": tier, "groups": [],
            "error": self.to_string(), "error_code": code,
            "http_status": self.status, "validation_url": self.validation_url,
        })
    }
}

impl From<String> for CloudCodeError {
    fn from(message: String) -> Self {
        Self { status: None, reason: String::new(), message, validation_url: String::new() }
    }
}

impl From<&str> for CloudCodeError {
    fn from(message: &str) -> Self { message.to_string().into() }
}

impl std::fmt::Display for CloudCodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match (self.status, self.reason.as_str()) {
            (Some(403), "VALIDATION_REQUIRED") => {
                if self.validation_url.is_empty() {
                    write!(f, "Google 要求验证此账号。请在官方 Antigravity 中登录并完成验证，再刷新额度。")
                } else {
                    write!(f, "Google 要求验证此账号。点击「验证账号」完成后，再刷新额度。")
                }
            }
            (Some(401), _) => write!(f, "登录凭据已失效（HTTP 401）。请重新登录此账号，或收录 IDE 中最新的登录凭据。"),
            (Some(403), _) => write!(f, "Google 拒绝额度查询（HTTP 403，{}）。请查看官方 Antigravity 对此账号的权限提示。{}",
                if self.reason.is_empty() { "PERMISSION_DENIED" } else { &self.reason }, self.message),
            (Some(status), _) => write!(f, "额度接口请求失败（HTTP {status}）：{}", self.message),
            _ => write!(f, "{}", self.message),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validation_required_keeps_action_without_claiming_wrong_oauth_client() {
        let body = json!({"error": {"status": "PERMISSION_DENIED", "message": "Verify your account to continue.",
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "VALIDATION_REQUIRED",
                "metadata": {"validation_url": "https://accounts.google.com/v3/signin/continue?fixture=1"}}]}});
        let error = CloudCodeError::from_http("retrieveUserQuotaSummary", 403, &body.to_string());
        let quota = error.quota_failure(&json!("Google AI Pro"), "fixture-time");
        assert_eq!(quota["error_code"], "VALIDATION_REQUIRED");
        assert_eq!(quota["http_status"], 403);
        assert_eq!(quota["tier"], "Google AI Pro");
        assert_eq!(quota["validation_url"], "https://accounts.google.com/v3/signin/continue?fixture=1");
        assert!(quota["error"].as_str().unwrap().contains("验证账号"));
        assert!(!quota["error"].as_str().unwrap().contains("gemini-cli"));
    }

    #[test]
    fn authentication_and_permission_failures_have_different_guidance() {
        let auth = CloudCodeError::from_http("loadCodeAssist", 401, "not JSON");
        assert!(auth.to_string().contains("凭据已失效"));
        let permission = CloudCodeError::from_http("loadCodeAssist", 403,
            r#"{"error":{"status":"PERMISSION_DENIED","message":"Account access denied"}}"#);
        assert!(permission.to_string().contains("Account access denied"));
        assert!(!permission.to_string().contains("gemini-cli"));
        assert!(!permission.to_string().contains("凭据已失效"));
        assert_eq!(permission.quota_failure(&Value::Null, "now")["validation_url"], "");
    }

    #[test]
    fn verification_links_must_use_the_google_accounts_https_origin() {
        for url in ["javascript:alert(1)", "http://accounts.google.com/", "https://accounts.google.com.evil.invalid/",
            "https://accounts.google.com@evil.invalid/", "https://user@accounts.google.com/",
            "https://accounts.google.com:444/", "https://evil.invalid/?next=https://accounts.google.com/"] {
            assert!(google_validation_url(url).is_none(), "{url}");
        }
        assert!(google_validation_url("https://accounts.google.com:443/v3/signin/continue?fixture=1").is_some());
    }
}
