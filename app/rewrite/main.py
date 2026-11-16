def handler(event, context):
    request = event["Records"][0]["cf"]["request"]
    uri = request["uri"]

    if "." in uri.split("/")[-1]:
        return request

    if "?" in uri or "#" in uri:
        return request

    if uri.endswith("/"):
        uri += "index.html"
    else:
        uri += "/index.html"

    request["uri"] = uri
    return request
