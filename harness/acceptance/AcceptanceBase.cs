// Base class for acceptance tests (the write-acceptance task). The harness copies
// it in; tests derived from it are never committed to an app repository.
// Black-box: rows are created and read only through HTTP, so the tests do not depend
// on how the agent modelled anything - only on the interface line in the brief.

using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using App.Tests.Postgres;

namespace Acceptance;

public abstract class AcceptanceBase(TemplateDatabase template) : EndpointTest(template)
{
    protected async Task<JsonElement> PostAsync(string url, object body, HttpStatusCode expect = HttpStatusCode.Created)
    {
        var r = await Client.PostAsJsonAsync(url, body, Ct);
        var text = await r.Content.ReadAsStringAsync(Ct);
        Assert.True(r.StatusCode == expect, $"POST {url} -> {(int)r.StatusCode}, expected {(int)expect}: {text}");
        return text.Length == 0 ? default : JsonDocument.Parse(text).RootElement.Clone();
    }

    protected async Task<JsonElement> SendAsync(HttpMethod m, string url, object? body, HttpStatusCode expect)
    {
        using var req = new HttpRequestMessage(m, url) { Content = body is null ? null : JsonContent.Create(body) };
        var r = await Client.SendAsync(req, Ct);
        var text = await r.Content.ReadAsStringAsync(Ct);
        Assert.True(r.StatusCode == expect, $"{m} {url} -> {(int)r.StatusCode}, expected {(int)expect}: {text}");
        return text.Length == 0 ? default : JsonDocument.Parse(text).RootElement.Clone();
    }

    protected async Task<JsonElement> GetAsync(string url, HttpStatusCode expect = HttpStatusCode.OK) =>
        await SendAsync(HttpMethod.Get, url, null, expect);

    protected async Task<int> CreateItemAsync(string title, int? priority = null) =>
        (await PostAsync("/items", new { title, priority })).GetProperty("id").GetInt32();

    protected async Task MarkDoneAsync(int id) =>
        await SendAsync(HttpMethod.Patch, $"/items/{id}", new { done = true }, HttpStatusCode.OK);

    protected static string[] Titles(JsonElement list) =>
        list.EnumerateArray().Select(e => e.GetProperty("title").GetString()!).ToArray();
}
