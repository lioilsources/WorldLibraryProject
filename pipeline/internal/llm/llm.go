// Package llm is a minimal OpenAI-compatible chat client that talks to the
// Python model-server over the Tailscale mesh.
package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Client targets {BaseURL}/v1/chat/completions.
type Client struct {
	BaseURL string
	APIKey  string
	Model   string
	HTTP    *http.Client
}

// New builds a client with a sane default timeout per attempt.
func New(baseURL, apiKey, model string) *Client {
	return &Client{
		BaseURL: baseURL,
		APIKey:  apiKey,
		Model:   model,
		HTTP:    &http.Client{Timeout: 120 * time.Second},
	}
}

type message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type chatRequest struct {
	Model       string    `json:"model"`
	Messages    []message `json:"messages"`
	Temperature float64   `json:"temperature"`
	MaxTokens   int       `json:"max_tokens"`
}

type chatResponse struct {
	Choices []struct {
		Message message `json:"message"`
	} `json:"choices"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// Chat performs one completion with bounded exponential-backoff retries on
// timeouts, 429 and 5xx. 4xx (other than 429) fails fast.
func (c *Client) Chat(ctx context.Context, system, user string, maxTokens int) (string, error) {
	body, err := json.Marshal(chatRequest{
		Model:       c.Model,
		Messages:    []message{{Role: "system", Content: system}, {Role: "user", Content: user}},
		Temperature: 0.2,
		MaxTokens:   maxTokens,
	})
	if err != nil {
		return "", err
	}

	var lastErr error
	backoff := 4 * time.Second
	for attempt := 0; attempt < 5; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(backoff):
			}
			backoff *= 2
		}
		text, retryable, err := c.once(ctx, body)
		if err == nil {
			return text, nil
		}
		lastErr = err
		if !retryable {
			return "", err
		}
	}
	return "", fmt.Errorf("llm: exhausted retries: %w", lastErr)
}

func (c *Client) once(ctx context.Context, body []byte) (string, bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.BaseURL+"/v1/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", false, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return "", true, err // network/timeout: retryable
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))

	if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
		return "", true, fmt.Errorf("llm: http %d: %s", resp.StatusCode, snippet(raw))
	}
	if resp.StatusCode >= 400 {
		return "", false, fmt.Errorf("llm: http %d: %s", resp.StatusCode, snippet(raw))
	}

	var cr chatResponse
	if err := json.Unmarshal(raw, &cr); err != nil {
		return "", false, fmt.Errorf("llm: decode: %w", err)
	}
	if cr.Error != nil {
		return "", false, fmt.Errorf("llm: api error: %s", cr.Error.Message)
	}
	if len(cr.Choices) == 0 {
		return "", true, fmt.Errorf("llm: empty choices")
	}
	return cr.Choices[0].Message.Content, false, nil
}

func snippet(b []byte) string {
	if len(b) > 300 {
		return string(b[:300])
	}
	return string(b)
}
