from cerebro import apiroutes

SAMPLE = """
@Controller('carts')
export class CartController {
  @Get()
  findAll() {}

  @Get(':id')
  findOne(@Param('id') id: string) {}

  @Post('lines')
  @UseGuards(AuthGuard)
  async addLine(@Body() dto: Dto) {}

  @Delete(':id')
  remove(@Param('id') id: string) {}
}
"""


def test_extract_nestjs_routes():
    eps = apiroutes.extract_file("cart.controller.ts", SAMPLE)
    by = {(e["method"], e["path"]): e for e in eps}
    assert ("GET", "/carts") in by
    assert ("GET", "/carts/:id") in by
    assert ("POST", "/carts/lines") in by
    assert ("DELETE", "/carts/:id") in by
    # handler attaches to the method even past an intervening decorator
    assert by[("POST", "/carts/lines")]["handler"] == "addLine"
    assert by[("GET", "/carts")]["handler"] == "findAll"


def test_bare_controller_and_empty_path():
    eps = apiroutes.extract_file("x.controller.ts", "@Controller()\nclass X {\n  @Get()\n  ping() {}\n}\n")
    assert eps == [
        {"method": "GET", "path": "/", "file": "x.controller.ts", "line": 3, "handler": "ping"}
    ]


def test_no_controller_no_routes():
    assert apiroutes.extract_file("plain.ts", "export const x = 1;\n") == []
