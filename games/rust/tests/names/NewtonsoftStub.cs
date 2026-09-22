// The lifted frame helper uses Carbon's JObject API. The isolated harness supplies only
// the two members that helper reaches, backed by the .NET runtime's JSON parser.
using System.Text.Json;

namespace Newtonsoft.Json.Linq
{
    internal sealed class JObject
    {
        private readonly JsonDocument _document;

        private JObject(JsonDocument document)
        {
            _document = document;
        }

        public static JObject Parse(string value)
        {
            return new JObject(JsonDocument.Parse(value));
        }

        public T Value<T>(string name)
        {
            JsonElement value;
            if (!_document.RootElement.TryGetProperty(name, out value)) return default(T);
            object result = value.ValueKind == JsonValueKind.String ? value.GetString() : value.ToString();
            return (T)result;
        }
    }
}
